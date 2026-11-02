"""Oversize-file handling: size check, manual-upload ledger, stop instructions."""

import os
import re
from datetime import datetime

from . import config, progress


def _max_upload_bytes():
    """The size limit in bytes, or 0 when the check is disabled."""
    return int(config.settings.max_upload_mb * 1024 * 1024) if config.settings.max_upload_mb else 0


def _mb(n_bytes):
    return f"{n_bytes / (1024 * 1024):.1f} MB"


def find_oversize_media(media):
    """[(path, kind, size_bytes)] for every file bigger than the limit (strictly greater)."""
    limit = _max_upload_bytes()
    if not limit:
        return []
    out = []
    for path, kind in media:
        try:
            size = os.path.getsize(path)
        except OSError:
            continue                      # unreadable here: the upload itself will report it
        if size > limit:
            out.append((path, kind, size))
    return out


def load_manual_uploads():
    """Source message ids you handled by hand, from the manual-uploads file (settings.manual_uploads_file).

    One id per line, optionally followed by the id of the message you posted in
    the channel (so later replies can link to it):
        1234
        1234 5678        # the second number is the destination message id
    Blank lines and anything after '#' are ignored.
    Returns (set_of_source_ids, {source_id: destination_id}).
    """
    done, mapped = set(), {}
    if not os.path.exists(config.settings.manual_uploads_file):
        return done, mapped
    with open(config.settings.manual_uploads_file, encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.replace(",", " ").split()
            src = re.sub(r"^message", "", parts[0], flags=re.IGNORECASE)
            if not src.isdigit():
                print(f"Warning: ignoring unreadable line in {os.path.basename(config.settings.manual_uploads_file)}: {raw.strip()!r}")
                continue
            done.add(src)
            if len(parts) > 1 and parts[1].isdigit():
                mapped[src] = int(parts[1])
    return done, mapped


def mark_manual_upload(source_id, rel_files):
    """Append `source_id` to the manual-uploads file (once). True if it is recorded afterwards.

    The line is "<id>    # comment"; load_manual_uploads() ignores the comment, and you can
    write the channel message id of your upload after the number to link replies to it.
    """
    try:
        done, _mapped = load_manual_uploads()
        if str(source_id) in done:
            return True
        existing = ""
        if os.path.exists(config.settings.manual_uploads_file):
            with open(config.settings.manual_uploads_file, encoding="utf-8") as f:
                existing = f.read()
        lead = "" if (not existing or existing.endswith("\n")) else "\n"
        header = "" if existing else (
            "# Source message ids you uploaded by hand (the script skips them).\n"
            "# Format:  <id>   or   <id> <channel message id of your upload>   # comment\n")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        files = ", ".join(rel_files)
        with open(config.settings.manual_uploads_file, "a", encoding="utf-8") as f:
            f.write(f"{lead}{header}{source_id}    # auto-added {stamp} after an oversize stop: upload {files} by hand\n")
            f.flush()
        return True
    except Exception as exc:
        print(f"Warning: could not write {config.settings.manual_uploads_file}: {type(exc).__name__}: {exc}")
        return False


def format_oversize_instructions(source_id, data, caption, oversize, last_dest_id, migrated, auto_marked=False):
    big = {os.path.abspath(p) for p, _k, _s in oversize}
    ts = data.get("timestamp")
    when = ts.strftime("%Y-%m-%d %H:%M") if ts else "unknown time"
    who = data.get("sender") or "unknown sender"
    lines = [
        "",
        "═" * 72,
        f"STOPPED: message id={source_id} has a file over the {_mb(_max_upload_bytes())} upload limit.",
        "═" * 72,
        "None of this message's files were uploaded by the script. It is logged in:",
        f"    {config.settings.failed_file}",
        "",
        f"Upload these file(s) to the channel by hand (original message: {when}, {who}):",
    ]
    for path, kind in data["media"]:
        ap = os.path.abspath(path)
        try:
            size = _mb(os.path.getsize(path))
        except OSError:
            size = "size unknown"
        lines.append(f"    [{kind}] {ap}  ({size}){'   <-- TOO LARGE' if ap in big else ''}")
    if caption and caption.strip():
        shown = caption if len(caption) <= 1500 else caption[:1500] + "…"
        lines += ["", "Caption to paste with it:"] + ["    | " + ln for ln in shown.splitlines()]
    where = (f"after the {migrated} message(s) already migrated"
             + (f" (the last one is message #{last_dest_id} in the channel)" if last_dest_id else ""))
    if auto_marked:
        lines += [
            "",
            "To resume:",
            f"  1. Upload the file(s) now, BEFORE running the script again, so they land {where}",
            "     and ahead of the messages that follow.",
            f"  2. Nothing to edit: message {source_id} is already recorded in",
            f"         {config.settings.manual_uploads_file}",
            "     so the next run skips it and continues with the following message.",
            f"     (Optional: write the channel message id of your upload after the number, e.g.",
            f"      \"{source_id} 5678\", so replies to this message link to it. To undo, delete the line.)",
            "  3. Run the script again.",
            "",
            "WARNING: if you run the script again BEFORE uploading, this message will be skipped.",
        ]
    else:
        lines += [
            "",
            "To resume:",
            f"  1. Upload the file(s) now, BEFORE running the script again, so they land {where}",
            "     and ahead of the messages that follow.",
            f"  2. Add this line to {config.settings.manual_uploads_file}:",
            f"         {source_id}",
            "     (optionally add the channel message id of your upload after it, e.g.",
            f"      \"{source_id} 5678\", so replies to this message link to it).",
            "  3. Run the script again. It continues with the next message.",
        ]
    lines += [
        "",
        f"All progress is saved ({migrated} message(s) migrated so far).",
        "═" * 72,
    ]
    return "\n".join(lines)


def halt_for_oversize(source_id, data, caption, oversize, id_map, ctx):
    """Log the message id, print the manual-upload instructions and request a clean stop."""
    reason = "FILE_TOO_LARGE: " + "; ".join(
        f"{os.path.relpath(p, config.settings.base_dir)} ({_mb(sz)} > {_mb(_max_upload_bytes())} limit)"
        for p, _k, sz in oversize
    )
    progress.log_failure(source_id, reason)
    last_dest = ctx.get("last_dest_id") or (max(id_map.values()) if id_map else None)
    auto = False
    if config.settings.auto_mark_manual_on_stop:
        auto = mark_manual_upload(source_id, [os.path.relpath(p, config.settings.base_dir) for p, _k in data["media"]])
    print(format_oversize_instructions(source_id, data, caption, oversize, last_dest, len(id_map), auto_marked=auto))
    ctx["halt"].append(source_id)
    ctx["stop"] = True


def print_oversize_dry_run(source_id, data, oversize):
    for path, kind, size in oversize:
        print(f"  [dry] ⚠ id={source_id:<6} OVERSIZE [{kind}] {os.path.abspath(path)} ({_mb(size)})"
              f" - a real run stops here")
