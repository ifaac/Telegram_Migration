"""The migration run: stream the export, send each message, resume, stop cleanly."""

import asyncio
import os
import sys

from lxml import etree
from telethon import TelegramClient

from . import bigfiles, cli, config, htmlexport, mediatools, progress, reaction_archive, replylinks, sending, uploads
from .htmlutil import css_classes
from .textutil import compose_message, split_with_entities, strip_with_entities
from .constants import NO_CAPTION_KINDS, STICKER_KINDS


async def process_file(client, channel, path, id_map, partial, pending_replies, stats, ctx,
                      only_pending=False):
    print(("Retrying deferred replies in " if only_pending else "Streaming ") + os.path.basename(path) + " ...")
    with open(path, "rb") as export_file:
        context = etree.iterparse(export_file, events=("end",), tag="div", html=True, encoding="utf-8")
        for _event, elem in context:
            if "message" in css_classes(elem):
                await _process_message(
                    elem, client, channel, id_map, partial, pending_replies, stats, ctx,
                    only_pending,
                )
                if ctx["stop"]:
                    break
    return ctx["stop"]


def _resolve_reply(data, source_id, id_map, pending_replies, ctx):
    reply_to = id_map.get(str(data["reply_src_id"])) if data["reply_src_id"] else None
    reply_fallback = ("", [])
    if not data["reply_src_id"] or reply_to is not None:
        return reply_to, reply_fallback, False

    target_src = str(data["reply_src_id"])
    if target_src in ctx["source_ids"] and target_src not in ctx["processed_source_ids"]:
        if not config.settings.dry_run:
            if pending_replies.get(source_id) != target_src:
                pending_replies[source_id] = target_src
                progress.save_pending_replies(pending_replies)
        else:
            print(f"  [dry] would defer reply id={source_id} until target id={target_src} is migrated")
        return None, reply_fallback, True

    fallback = f"[Original reply target #{target_src} is unavailable in the destination]\n"
    if data.get("reply_preview"):
        fallback += f"Original reply preview: {data['reply_preview']}\n"
    return None, (fallback + "\n", []), False


def _print_dry_run(source_id, data, text, entities, first_limit, reply_to,
                   kind_summary, stats, pending_replies):
    chunks = split_with_entities(text, entities, first_limit, config.settings.text_limit)
    flag = "↪" if reply_to else ("?" if data["reply_src_id"] else " ")
    fwd = "F" if data["forward_source"] else " "
    preview = text[:80].replace("\n", "⏎")
    extra = f" +{len(chunks) - 1} part(s)" if len(chunks) > 1 else ""
    print(f"  [dry] {flag}{fwd} id={source_id:<6} ({kind_summary:<15}) {preview!r}{extra}")
    stats["sent"] += 1
    pending_replies.pop(source_id, None)


async def _send_migrated_message(client, channel, data, source_id, text, entities,
                                 first_limit, reply_to, annotation, partial,
                                 pending_replies, id_map, stats, ctx, kind_summary, when):
    entities = await sending.resolve_mentions(client, entities)
    chunks = split_with_entities(text, entities, first_limit, config.settings.text_limit)
    state = {"src": source_id, "force_reconstruction": bool(data["reply_src_id"])}
    if source_id in partial:
        state.update(partial[source_id])

    new_id = await sending.send_one(
        client, channel, data, chunks, reply_to, state, annotation, source_id
    )
    if new_id:
        id_map[source_id] = int(new_id)
        ctx["last_dest_id"] = int(new_id)
        progress.append_id_map(source_id, new_id)
        stats["sent"] += 1
        if state.get("native"):
            stats["forwarded"] += 1
        was_pending = source_id in pending_replies
        pending_replies.pop(source_id, None)
        if was_pending:
            progress.save_pending_replies(pending_replies)
        print(f"  [{stats['sent']:>4}] id={source_id:<6} → {new_id:<7}  ({kind_summary})  {when}")
        return

    stats["failed"] += 1
    if data["reply_src_id"]:
        ctx["audit"].append({
            "src": source_id,
            "source_reply_to": str(data["reply_src_id"]),
            "verified": False,
            "status": "send_failed",
        })


async def _process_message(elem, client, channel, id_map, partial, pending_replies,
                           stats, ctx, only_pending):
    source_id = None
    mark_completed = False
    try:
        raw_elem_id = elem.get("id", "")
        if raw_elem_id.startswith("message") and raw_elem_id[7:].isdigit():
            source_id = raw_elem_id[7:]
        data = htmlexport.extract_message_data(elem)
        if data is None:
            mark_completed = bool(source_id)
            return
        source_id = str(data["msg_id"])

        if data["sender"]:
            ctx["last_sender"] = data["sender"]
        elif data["joined"] and config.settings.preserve_sender_names:
            data["sender"] = ctx["last_sender"]

        if data["reply_src_id"]:
            ctx["reply_map"][source_id] = str(data["reply_src_id"])
        if data.get("reactions") and source_id not in ctx["reaction_map"]:
            ctx["reaction_map"][source_id] = data["reactions"]

        if only_pending and source_id not in pending_replies:
            return

        if source_id in id_map or source_id in ctx["manual_done"]:
            was_pending = source_id in pending_replies
            pending_replies.pop(source_id, None)
            if was_pending and not config.settings.dry_run:
                progress.save_pending_replies(pending_replies)
            mark_completed = True
            return

        if stats["sent"] + stats["failed"] >= config.settings.batch_limit:
            print(f"Batch limit reached ({config.settings.batch_limit}). Restart to continue.")
            ctx["stop"] = True
            return

        has_content = bool(data["body_text"].strip() or data["media"])
        if not has_content:
            stats["skipped"] += 1
            was_pending = source_id in pending_replies
            pending_replies.pop(source_id, None)
            if was_pending and not config.settings.dry_run:
                progress.save_pending_replies(pending_replies)
            mark_completed = True
            return

        timestamp = data["timestamp"]
        watermark = (f"[{timestamp.strftime('%Y-%m-%d %H:%M')}]\n", []) if timestamp else ("", [])
        sender = (f"{data['sender']}:\n", []) if data["sender"] else ("", [])
        forward = (data["forward_header"], [])

        reply_to, reply_fallback, deferred = _resolve_reply(
            data, source_id, id_map, pending_replies, ctx
        )
        if deferred:
            return

        text, entities = compose_message(
            watermark, sender, forward, data["body_text"], data["body_entities"],
            extra_before_body=reply_fallback,
        )
        text, entities = strip_with_entities(text, entities)

        oversize = bigfiles.find_oversize_media(data["media"])
        if oversize:
            if config.settings.dry_run:
                bigfiles.print_oversize_dry_run(source_id, data, oversize)
                ctx["oversize_report"].append(source_id)
                mark_completed = True
                return
            bigfiles.halt_for_oversize(source_id, data, text, oversize, id_map, ctx)
            return

        annotation_text, annotation_entities = compose_message(watermark, sender, ("", []), "", [])
        annotation_text, annotation_entities = strip_with_entities(annotation_text, annotation_entities)
        annotation = [(annotation_text, annotation_entities)] if annotation_text.strip() else None

        kinds = [kind for _path, kind in data["media"]]
        captionable = bool(kinds) and kinds[0] not in NO_CAPTION_KINDS
        has_sticker = any(kind in STICKER_KINDS for kind in kinds)
        first_limit = config.settings.text_limit if has_sticker else (
            config.settings.caption_limit if (captionable or len(kinds) > 1)
            else config.settings.text_limit
        )
        kind_summary = ",".join(kinds) or "text"
        when = timestamp.strftime("%Y-%m-%d %H:%M") if timestamp else "?"

        if config.settings.dry_run:
            _print_dry_run(
                source_id, data, text, entities, first_limit, reply_to,
                kind_summary, stats, pending_replies,
            )
            mark_completed = True
            return

        await _send_migrated_message(
            client, channel, data, source_id, text, entities, first_limit,
            reply_to, annotation, partial, pending_replies, id_map, stats,
            ctx, kind_summary, when,
        )
        mark_completed = True
        await asyncio.sleep(config.settings.send_delay_seconds)
    except Exception as exc:
        mid = ("message" + source_id) if source_id else elem.get("id", "?")
        print(f"    failed {mid}: {type(exc).__name__}: {exc}")
        progress.log_failure(mid, f"{type(exc).__name__}: {exc}")
        stats["failed"] += 1
        mark_completed = True
    finally:
        if source_id and mark_completed:
            ctx["processed_source_ids"].add(source_id)
        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]


async def run_async(settings=None, **overrides):
    """Run (or resume) one migration pass.

    `settings` is a :class:`~telegram_migration.config.Settings`; when omitted it is read
    from the environment (``Settings.from_env()``). Keyword arguments override single
    settings, e.g. ``run_async(dry_run=False)``. Settings are scoped to this async task,
    so concurrent runs remain isolated. Raises :class:`~telegram_migration.config.ConfigError` when the
    credentials or the destination are missing.
    """
    resolved = config.resolve_settings(settings, **overrides).validate()
    with config.use_settings(resolved):
        return await _run_migration()


async def _run_migration():
    uploads.configure_send_file_compat()
    mediatools.enable_hachoir()

    drive_root = os.path.abspath("/content/drive")
    configured_dir = os.path.abspath(config.settings.base_dir)
    using_drive = configured_dir == drive_root or configured_dir.startswith(drive_root + os.sep)
    if "google.colab" in sys.modules and using_drive and not os.path.isdir("/content/drive/MyDrive"):
        from google.colab import drive
        drive.mount("/content/drive")

    # FFmpeg/ffprobe are installed lazily only if a .webm video sticker needs
    # dimensions/duration and no usable ffprobe is already available.
    files = htmlexport.discover_export_files(config.settings.base_dir)
    if not files:
        print(f"No messages.html / messages2.html ... found in: {config.settings.base_dir}")
        return

    id_map = progress.load_id_map()
    manual_done, manual_map = bigfiles.load_manual_uploads()
    for _src, _dst in manual_map.items():
        id_map.setdefault(_src, _dst)         # so replies to your manual uploads link to them
    if manual_done:
        _ids = sorted(manual_done, key=int)
        _shown = ", ".join(_ids[:20]) + (" ..." if len(_ids) > 20 else "")
        print(f"{len(manual_done)} message(s) marked as uploaded by hand and SKIPPED by the script (ids: {_shown}).")
        print(f"  Make sure they are really in the channel; remove a line from {os.path.basename(config.settings.manual_uploads_file)} to have the script send it.")
    partial = progress.load_partial()
    pending_replies = progress.load_pending_replies()
    source_ids = progress.collect_source_ids(files)           # one cheap pass; len() is the message count
    total = len(source_ids)
    reaction_map = reaction_archive.load_json_reactions(files, source_ids)

    print(f"Export files: {len(files)}   Already processed: {len(id_map)}   File(s) contain: {total} messages")
    print(f"{len(pending_replies)} deferred replies are queued.")

    # Session renamed to '_bot' to keep it isolated from any user-account session.
    client = TelegramClient(config.settings.session_name, config.settings.api_id, config.settings.api_hash)
    await client.start(bot_token=config.settings.bot_token)
    print("Logged in officially as a bot.")

    try:
        channel = await client.get_entity(config.settings.channel_peer)
    except Exception as e:
        print(f"Channel error: {e}")
        await client.disconnect()
        return

    if not cli.confirm_target(
            channel, max(0, total - len(set(id_map) | manual_done)), len(files), config.get_settings()):
        print("Aborted by user.")
        await client.disconnect()
        return

    stats = {"sent": 0, "skipped": 0, "failed": 0, "forwarded": 0, "reaction_notes": 0}
    ctx = {
        "last_sender": "",
        "stop": False,
        "source_ids": source_ids,
        "processed_source_ids": set(),
        "reaction_map": reaction_map,
        "reply_map": {},          # source reply targets, collected during the main pass
        "audit": [],              # reply-audit records for this run
        "manual_done": manual_done,   # ids you uploaded by hand (never sent by the script)
        "halt": [],                   # non-empty => an oversize file stopped the run
        "oversize_report": [],        # dry run: every message with an oversize file
        "last_dest_id": None,
    }

    for path in files:
        if await process_file(client, channel, path, id_map, partial, pending_replies, stats, ctx):
            break

    # Second pass: deferred replies whose target has now been migrated. Any whose
    # target ultimately failed are sent with the archived reply preview instead.
    if not ctx["stop"] and pending_replies:
        # Repeat if a deferred reply was itself a parent for another deferred reply.
        while pending_replies and not ctx["stop"]:
            before_pending = len(pending_replies)
            before_completed = len(ctx["processed_source_ids"])
            pending_ctx = dict(ctx)          # shallow copy: shares the maps/lists above
            pending_ctx["last_sender"] = ""
            pending_ctx["stop"] = False
            for path in files:
                if await process_file(client, channel, path, id_map, partial, pending_replies,
                                      stats, pending_ctx, only_pending=True):
                    ctx["stop"] = True
                    break
            ctx["processed_source_ids"].update(pending_ctx["processed_source_ids"])
            if len(pending_replies) >= before_pending and len(ctx["processed_source_ids"]) <= before_completed:
                break

    if ctx["halt"]:
        # Clean stop: everything already sent is in the id map, deferred replies and
        # partial-send states are on disk. No audit/reaction pass, no further sends.
        progress.save_pending_replies(pending_replies)
        print(f"Stopped at message id={ctx['halt'][0]}: sent={stats['sent']} this run, progress saved.")
        print("Follow the instructions above, then run the script again to resume.")
        await client.disconnect()
        return

    if not reaction_map:
        print("No reaction metadata was found. Telegram Desktop HTML exports generally do not expose full reaction data.")
        print("To archive available reactions, place the matching Telegram JSON export (result.json or messages.json, with reaction fields) directly in the export folder.")

    note_map = progress.load_reaction_note_map()
    if not config.settings.dry_run:
        reaction_archive.persist_reaction_source_metadata(reaction_map)
        await replylinks.verify_all_reply_links(client, channel, ctx["reply_map"], id_map, ctx["audit"])
        progress.write_reply_audit(ctx["audit"])
        if not ctx["stop"]:
            await reaction_archive.restore_reaction_archive_notes(client, channel, id_map, reaction_map, note_map, stats)

    if config.settings.dry_run and ctx["oversize_report"]:
        print()
        print(f"{len(ctx['oversize_report'])} message(s) have files over {bigfiles.format_mb(bigfiles.max_upload_bytes())} "
              f"(ids: {', '.join(ctx['oversize_report'])}). A real run stops at the first one.")

    print()
    print("═" * 60)
    print(f"Done.  sent={stats['sent']}   skipped={stats['skipped']}   failed={stats['failed']}   "
          f"native_forwards={stats['forwarded']}   reaction_notes={stats.get('reaction_notes', 0)}")
    print(f"Reply audit: {config.settings.reply_audit_file}")
    if pending_replies:
        print(f"Deferred replies still waiting for a target mapping: {len(pending_replies)} (saved in {config.settings.pending_replies_file})")
    if stats["failed"]:
        print(f"Failed IDs are logged in {config.settings.failed_file}")
    print("═" * 60)
    await client.disconnect()
