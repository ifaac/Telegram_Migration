"""Archive reaction metadata from a companion JSON export as text notes."""

import asyncio
import json
import os

from telethon.errors import FloodWaitError

try:
    import ijson
except ImportError:  # optional: only used to stream a very large result.json
    ijson = None

from . import config, progress
from .textutil import sanitize_text


def _chat_message_candidates(root):
    """Find plausible message arrays in Telegram Desktop JSON export layouts."""
    candidates = []
    if isinstance(root, dict) and isinstance(root.get("messages"), list):
        candidates.append({"name": root.get("name") or root.get("title") or "", "messages": root["messages"], "direct": True})
    chats = root.get("chats") if isinstance(root, dict) else None
    if isinstance(chats, dict):
        chats = chats.get("list")
    if isinstance(chats, list):
        for chat in chats:
            if isinstance(chat, dict) and isinstance(chat.get("messages"), list):
                candidates.append({"name": chat.get("name") or chat.get("title") or "", "messages": chat["messages"], "direct": False})
    return candidates


def load_json_reactions(files, source_ids):
    """Load reaction fields from companion result.json/messages.json when present.

    The preferred path streams single-chat `messages.item` arrays via ijson. For
    other layouts it uses a bounded-size JSON fallback and chooses the chat with
    the strongest ID overlap with the HTML export.
    """
    for filename in config.settings.json_reaction_file_candidates:
        path = os.path.join(config.settings.base_dir, filename)
        if not os.path.isfile(path):
            continue
        found_any = False
        streamed = {}
        if ijson is not None:
            try:
                with open(path, "rb") as f:
                    for row in ijson.items(f, "messages.item"):
                        found_any = True
                        if not isinstance(row, dict):
                            continue
                        mid = row.get("id")
                        if mid is None:
                            continue
                        mid = str(mid)
                        if mid not in source_ids:
                            continue
                        raw = row.get("reactions", row.get("reaction_counts", row.get("reactions_count")))
                        normalized = normalize_reactions(raw)
                        if normalized:
                            streamed[mid] = normalized
                if found_any and streamed:
                    print(f"Loaded reaction metadata from {filename} ({len(streamed)} message(s)).")
                    return streamed
                if found_any:
                    print(f"Found message rows in {filename}, but no matching reaction data in the streaming pass; checking its export layout.")
            except Exception as exc:
                print(f"Warning: streaming reaction read from {filename} failed: {exc}")

        try:
            size_mb = os.path.getsize(path) / (1024 * 1024)
            if size_mb > config.settings.max_json_fallback_mb:
                print(f"Warning: {filename} is {size_mb:.0f} MB and is not a simple streaming chat JSON; skipping JSON reaction import to avoid excessive memory use.")
                continue
            with open(path, "r", encoding="utf-8") as f:
                root = json.load(f)
            candidates = _chat_message_candidates(root)
            if not candidates:
                print(f"Warning: no recognized message array found in {filename}; reaction import skipped.")
                continue
            ranked = []
            for candidate in candidates:
                rows = candidate["messages"]
                ids = {str(m.get("id")) for m in rows if isinstance(m, dict) and m.get("id") is not None}
                overlap = len(ids & source_ids)
                ranked.append((overlap, candidate))
            ranked.sort(key=lambda pair: pair[0], reverse=True)
            if not ranked or ranked[0][0] == 0:
                print(f"Warning: no message IDs from {filename} match the HTML export.")
                continue
            if len(ranked) > 1 and ranked[0][0] == ranked[1][0] and not ranked[0][1].get("direct"):
                print(f"Warning: multiple chats in {filename} match the HTML IDs equally; not guessing which chat's reactions to use.")
                continue
            selected = ranked[0][1]
            reaction_map = {}
            for row in selected["messages"]:
                if not isinstance(row, dict) or row.get("id") is None:
                    continue
                mid = str(row["id"])
                if mid not in source_ids:
                    continue
                raw = row.get("reactions", row.get("reaction_counts", row.get("reactions_count")))
                normalized = normalize_reactions(raw)
                if normalized:
                    reaction_map[mid] = normalized
            print(f"Loaded reaction metadata from {filename} ({len(reaction_map)} message(s), chat {selected.get('name')!r}).")
            return reaction_map
        except Exception as exc:
            print(f"Warning: could not read reaction metadata from {filename}: {type(exc).__name__}: {exc}")
    return {}


def format_reaction_archive_note(reactions):
    pieces = []
    for reaction in reactions:
        label = str(reaction.get("label") or "reaction").strip()
        count = reaction.get("count")
        piece = label + (f" × {count}" if count is not None else "")
        actors = reaction.get("actors") or []
        if actors:
            piece += " (listed actors: " + ", ".join(str(a) for a in actors[:20]) + ")"
            if len(actors) > 20:
                piece += f" and {len(actors) - 20} more"
        pieces.append(piece)
    if not pieces:
        return ""
    return "Archived reaction metadata from the source export (not live Telegram reaction totals):\n" + "\n".join(pieces)


def persist_reaction_source_metadata(reaction_map):
    """Write normalized export reaction details to a deduplicated JSONL sidecar."""
    if not reaction_map:
        return
    seen = set()
    if os.path.exists(config.settings.reaction_source_file):
        with open(config.settings.reaction_source_file, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    if row.get("record_type") == "source_reactions":
                        seen.add(str(row.get("src")))
                except Exception:
                    continue
    for src, reactions in reaction_map.items():
        if str(src) in seen:
            continue
        progress._append_jsonl(config.settings.reaction_source_file, {
            "record_type": "source_reactions",
            "src": str(src),
            "reactions": reactions,
        })


async def restore_reaction_archive_notes(client, channel, id_map, reaction_map, note_map, stats):
    if not config.settings.preserve_reactions_as_annotations or not reaction_map:
        return
    print(f"Archiving reactions for {len(reaction_map)} source message(s)...")
    for source_id, reactions in reaction_map.items():
        if source_id not in id_map or source_id in note_map:
            continue
        note_text = format_reaction_archive_note(reactions)
        if not note_text:
            continue
        try:
            # Sanitise and disable markdown parsing: a custom reaction label may
            # contain * or _, which Telethon would otherwise treat as formatting.
            sent = await client.send_message(
                channel, sanitize_text(note_text),
                reply_to=id_map[source_id], parse_mode=None, link_preview=False,
            )
            note_id = int(sent.id)
            note_map[source_id] = note_id
            progress._append_jsonl(config.settings.reaction_note_map_file, {"src": str(source_id), "note_id": note_id})
            progress._append_jsonl(config.settings.reaction_archive_file, {
                "src": str(source_id),
                "destination_message_id": int(id_map[source_id]),
                "reaction_note_id": note_id,
                "reactions": reactions,
            })
            stats["reaction_notes"] = stats.get("reaction_notes", 0) + 1
            print(f"  reaction metadata archived for source id={source_id} → note {note_id}")
            await asyncio.sleep(config.settings.send_delay_seconds)
        except FloodWaitError as fw:
            print(f"  FloodWait while archiving reactions: {fw.seconds}s; stopping reaction annotation pass for this run.")
            await asyncio.sleep(fw.seconds + 5)
            break
        except Exception as exc:
            progress._append_jsonl(config.settings.reaction_archive_file, {
                "src": str(source_id), "destination_message_id": int(id_map[source_id]),
                "status": "note_failed", "error": f"{type(exc).__name__}: {exc}", "reactions": reactions,
            })
            print(f"  reaction-note failed for source id={source_id}: {type(exc).__name__}: {exc}")


def _reaction_actor_names(value):
    if not isinstance(value, list):
        return []
    out = []
    for actor in value:
        if isinstance(actor, str):
            name = actor.strip()
        elif isinstance(actor, dict):
            name = str(actor.get("name") or actor.get("from") or actor.get("title") or
                       actor.get("username") or actor.get("from_id") or actor.get("id") or "").strip()
        else:
            name = str(actor).strip()
        if name and name not in out:
            out.append(name)
    return out


def normalize_reactions(raw):
    """Normalize common Telegram Desktop JSON reaction shapes without losing raw data."""
    if raw is None:
        return []
    if isinstance(raw, dict) and "label" in raw and "count" in raw:
        item = dict(raw)
        item.setdefault("actors", [])
        item.setdefault("source", "export")
        return [item]
    if isinstance(raw, list):
        out = []
        for item in raw:
            out.extend(normalize_reactions(item))
        return out
    if isinstance(raw, str):
        value = raw.strip()
        return [{"label": value, "count": None, "actors": [], "raw": raw, "source": "json"}] if value else []
    if not isinstance(raw, dict):
        return []

    # Nested containers seen in different exporter versions.
    for container_key in ("results", "reactions", "reaction_counts", "items", "list"):
        if container_key in raw and isinstance(raw[container_key], (list, dict)):
            nested = normalize_reactions(raw[container_key])
            if nested:
                return nested

    label = (raw.get("emoji") or raw.get("emoticon") or raw.get("label") or
             raw.get("reaction") or raw.get("reaction_emoji") or "")
    reaction_type = str(raw.get("type") or raw.get("kind") or "")
    custom_id = raw.get("document_id", raw.get("custom_emoji_id", raw.get("emoji_id")))
    if not label and custom_id is not None:
        label = f"custom emoji ID {custom_id}"
    if isinstance(label, dict):
        label = label.get("emoji") or label.get("emoticon") or label.get("label") or ""
    label = str(label).strip() if label is not None else ""

    count = None
    for key in ("count", "total", "reaction_count", "number"):
        value = raw.get(key)
        if isinstance(value, int):
            count = value
            break
        if isinstance(value, str) and value.isdigit():
            count = int(value)
            break

    actors = _reaction_actor_names(raw.get("recent") or raw.get("actors") or
                                   raw.get("users") or raw.get("reactors"))
    if label:
        return [{
            "label": label,
            "count": count,
            "actors": actors,
            "type": reaction_type or ("custom_emoji" if custom_id is not None else "emoji"),
            "raw": raw,
            "source": "json",
        }]

    # Some export tools encode reactions as {"👍": 12, "❤️": 4}.
    out = []
    for key, value in raw.items():
        if isinstance(value, (int, str)) and (isinstance(value, int) or str(value).isdigit()):
            out.append({"label": str(key), "count": int(value), "actors": [], "raw": {key: value}, "source": "json"})
    return out
