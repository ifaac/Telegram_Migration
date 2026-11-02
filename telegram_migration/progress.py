"""On-disk progress: id map, partial-send state, deferred replies, failure log, export discovery."""

import json
import os
import re

from . import config
from .constants import HTML_FILE_PATTERN


def load_id_map():
    out = {}
    if not os.path.exists(config.settings.idmap_file):
        return out
    with open(config.settings.idmap_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                out[str(r["src"])] = int(r["dst"])
            except Exception:
                continue
    return out


def append_id_map(src, dst):
    with open(config.settings.idmap_file, "a", encoding="utf-8") as f:
        f.write(json.dumps({"src": str(src), "dst": int(dst)}) + "\n")
        f.flush()


def load_partial():
    """Load per-message partial-send state from PARTIAL_FILE.

    The return value is a dict-of-dicts: {src: state_dict}. Records written
    by earlier versions ({"src": ..., "text_id": N}) are upgraded on load to
    {"text_id": N}. For every src the last record wins, so an interrupted
    run resumes exactly where it stopped, at the granularity of the smallest
    unit that was persisted (one text chunk, one album item, one media item,
    or one native forward).
    """
    out = {}
    if not os.path.exists(config.settings.partial_file):
        return out
    with open(config.settings.partial_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue
            src = record.get("src")
            if src is None:
                continue
            src = str(src)
            merged = out.get(src, {})
            if isinstance(record.get("state"), dict):
                merged.update(record["state"])
            elif "text_id" in record:
                # Legacy single-field record from an earlier version.
                try:
                    merged["text_id"] = int(record["text_id"])
                except (TypeError, ValueError):
                    continue
            out[src] = merged
    return out


def save_state(src, state):
    """Append a full snapshot of this message's send state to PARTIAL_FILE.

    The file is append-only; the last record for a given `src` is the
    authoritative one on reload. Persisting the whole state (not just
    text_id) is what lets the migration resume after a crash anywhere
    inside a multi-step send without re-posting text, media, album items,
    or a native forward.
    """
    snapshot = {
        key: value
        for key, value in state.items()
        if key not in ("src", "force_reconstruction")
    }
    try:
        payload = json.loads(json.dumps(snapshot, ensure_ascii=False, default=int))
    except (TypeError, ValueError) as exc:
        print(f"Warning: could not serialise send state for src={src}: {exc}")
        return
    try:
        with open(config.settings.partial_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"src": str(src), "state": payload}) + "\n")
            f.flush()
    except Exception as exc:
        print(f"Warning: could not persist send state for src={src}: "
              f"{type(exc).__name__}: {exc}")


def log_failure(msg_id, reason):
    reason = " ".join(str(reason).split())
    with open(config.settings.failed_file, "a", encoding="utf-8") as f:
        f.write(f"{msg_id}\t{reason}\n")
        f.flush()


def export_files():
    if not os.path.isdir(config.settings.base_dir):
        return []
    pat = re.compile(HTML_FILE_PATTERN)
    found = []
    for name in os.listdir(config.settings.base_dir):
        m = pat.match(name)
        if m:
            found.append((int(m.group(1) or 1), os.path.join(config.settings.base_dir, name)))
    return [p for _, p in sorted(found)]


def collect_source_ids(files):
    """One cheap regex pass over every HTML page: all real message ids.

    The number of ids is also the message count, so no separate counting pass
    is needed. Service messages (id="message-N") never match.
    """
    out = set()
    id_re = re.compile(rb'''id=["']message(\d+)["']''')
    for path in files:
        carry = b""
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                data = carry + chunk
                for value in id_re.findall(data):
                    out.add(value.decode("ascii", errors="ignore"))
                carry = data[-128:]
    return out


def load_pending_replies():
    if not os.path.exists(config.settings.pending_replies_file):
        return {}
    try:
        with open(config.settings.pending_replies_file, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return {str(k): str(v) for k, v in obj.items()} if isinstance(obj, dict) else {}
    except Exception as exc:
        print(f"Warning: could not read pending replies file: {exc}")
        return {}


def save_pending_replies(pending):
    """Atomically save deferred replies without forcing a slow Drive fsync.

    Persistence failures are reported but not raised into the message-send path:
    once Telegram has accepted a message, a local sidecar-write failure must not
    cause that successful send to be logged/retried as a failed upload.
    """
    tmp = config.settings.pending_replies_file + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
            f.flush()
        os.replace(tmp, config.settings.pending_replies_file)
        return True
    except Exception as exc:
        print(f"Warning: could not persist deferred-reply state: {type(exc).__name__}: {exc}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def _append_jsonl(path, record):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        f.flush()


def write_reply_audit(records):
    """Rewrite the reply audit from this run's records (no duplicate lines across runs)."""
    tmp = config.settings.reply_audit_file + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            f.flush()
        os.replace(tmp, config.settings.reply_audit_file)
        return True
    except Exception as exc:
        print(f"Warning: could not write the reply audit: {type(exc).__name__}: {exc}")
        return False


def load_reaction_note_map():
    out = {}
    if not os.path.exists(config.settings.reaction_note_map_file):
        return out
    with open(config.settings.reaction_note_map_file, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                out[str(row["src"])] = int(row["note_id"])
            except Exception:
                continue
    return out
