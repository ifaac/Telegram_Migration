"""The migration run: stream the export, send each message, resume, stop cleanly."""

import asyncio
import os
import sys

from lxml import etree
from telethon import TelegramClient
from telethon.tl import types as tl

from . import album, bigfiles, cli, config, customemoji, htmlexport, mediatools, progress, reaction_archive, replylinks, sending, uploads
from .htmlutil import css_classes
from .textutil import compose_message, compose_message_header, format_archive_timestamp, split_with_entities, strip_with_entities, u16len

async def process_file(client, channel, path, id_map, partial, pending_replies, stats, ctx,
                      only_pending=False):
    print(("Retrying deferred replies in " if only_pending else "Streaming ") + os.path.basename(path) + " ...")
    ctx["current_export_date"] = None
    ctx["last_forward_name"] = ""
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


def _print_dry_run(source_id, data, text, entities, first_limit, reply_to,
                   kind_summary, stats, pending_replies):
    chunks = split_with_entities(text, entities, first_limit, config.settings.text_limit)
    flag = "↪" if reply_to else ("?" if replylinks.source_reply_target(data) else " ")
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
    state = {
        "src": source_id,
        "force_reconstruction": bool(replylinks.source_reply_target(data)),
    }
    if source_id in partial:
        state.update(partial[source_id])

    new_id = await sending.send_one(
        client, channel, data, chunks, reply_to, state, annotation, source_id,
    )
    if new_id:
        if isinstance(new_id, (list, tuple)):
            source_ids = data.get("_album_source_ids") or [source_id]
            primary_id = album.map_destination_ids(
                source_ids, new_id, id_map, progress.append_id_map
            )
            ctx["last_dest_id"] = primary_id
            stats["sent"] += len(source_ids)
            for member_src in source_ids:
                was_pending = str(member_src) in pending_replies
                pending_replies.pop(str(member_src), None)
                if was_pending:
                    progress.save_pending_replies(pending_replies)
            print(
                f"  [{stats['sent']:>4}] album "
                f"{source_ids[0]}–{source_ids[-1]} → {primary_id} "
                f"({len(source_ids)} media items)  {when}"
            )
            return primary_id

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
        return int(new_id)

    stats["failed"] += 1
    reply_target = replylinks.source_reply_target(data)
    if reply_target:
        ctx["audit"].append({
            "src": source_id,
            "source_reply_to": reply_target,
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
        if source_id and source_id in ctx["processed_source_ids"]:
            mark_completed = True
            return
        classes = css_classes(elem)
        if "service" in classes:
            date_separator = htmlexport.extract_date_separator(elem)
            if date_separator is not None:
                ctx["current_export_date"] = date_separator
                htmlexport.reset_forwarded_context(ctx)
                return
            data = (htmlexport.extract_service_event_data(
                elem, ctx.get("current_export_date")
            ) if config.settings.preserve_service_messages else None)
        else:
            data = htmlexport.extract_message_data(elem)

        if data is None:
            htmlexport.reset_forwarded_context(ctx)
            if "service" in classes and source_id:
                stats["skipped"] += 1
            mark_completed = bool(source_id)
            return
        source_id = str(data["msg_id"])
        customemoji.apply_custom_emoji_entities(data, ctx["inline_custom_emoji_ids"])

        if data["sender"]:
            ctx["last_sender"] = data["sender"]
        elif data["joined"] and config.settings.preserve_sender_names:
            data["sender"] = ctx["last_sender"]

        htmlexport.inherit_forwarded_sender(data, ctx)

        inferred_group = ctx.get("inferred_groups", {}).get(source_id)
        prepared_album = album.prepare_inferred_group(
            data, inferred_group,
            id_map, ctx["manual_done"], partial, progress.append_id_map,
        )
        if inferred_group and not prepared_album:
            member_ids = [str(member["msg_id"]) for member in inferred_group]
            mapped_ids = [
                member_id for member_id in member_ids
                if member_id in id_map or member_id in ctx["manual_done"]
            ]
            if mapped_ids and len(mapped_ids) != len(member_ids):
                # Some members were already migrated individually (or marked
                # manually complete). Sending the remaining members normally
                # would scatter the source album across separate Telegram posts.
                # Do not guess, duplicate, or silently fall back to individual
                # uploads: stop here and require an explicit repair decision.
                reason = (
                    f"Cannot reconstruct inferred album {member_ids[0]}–{member_ids[-1]}: "
                    f"{len(mapped_ids)} of {len(member_ids)} source messages are already "
                    "mapped/completed. Refusing to scatter the remaining media."
                )
                print("    album recovery conflict: " + reason)
                progress.log_failure("album:" + member_ids[0], reason)
                stats["failed"] += 1
                ctx["stop"] = True
                return

        reply_records = inferred_group if prepared_album else [data]
        group_reply_target = replylinks.source_reply_target(reply_records[0]) if reply_records else None
        if group_reply_target: ctx["reply_map"].update({str(row["msg_id"]): group_reply_target for row in reply_records})
        if data.get("reactions") and source_id not in ctx["reaction_map"]:
            ctx["reaction_map"][source_id] = data["reactions"]

        if only_pending and source_id not in pending_replies:
            return

        if data.get("_inferred_album") and all(
                member_id in id_map or member_id in ctx["manual_done"]
                for member_id in data["_album_source_ids"]
        ):
            if all(member_id in id_map for member_id in data["_album_source_ids"]):
                await album.verify_mapped_album(
                    client, channel, data, id_map, partial, source_id
                )
            mark_completed = True
            return

        if source_id in id_map or source_id in ctx["manual_done"]:
            was_pending = source_id in pending_replies
            pending_replies.pop(source_id, None)
            if was_pending and not config.settings.dry_run:
                progress.save_pending_replies(pending_replies)
            mark_completed = True
            return

        batch_cost = len(data.get("_album_source_ids", [])) or 1
        batch_progress = stats["sent"] + stats["failed"]
        if batch_progress and batch_progress + batch_cost > config.settings.batch_limit:
            print(f"Batch limit reached ({config.settings.batch_limit}). Restart to continue.")
            ctx["stop"] = True
            return

        has_content = bool(
            data["body_text"].strip() or data["media"] or data.get("is_forwarded")
        )
        if not has_content:
            data["body_text"] = (
                "[Empty message in source export; no text or media were included]"
            )
            print(
                f"  empty source message id={source_id}: "
                "preserving as an archival placeholder"
            )

        timestamp = data["timestamp"]
        if data.get("service_event"):
            header = compose_message_header(
                timestamp, "Telegram service event", date_only=True
            )
        else:
            header = compose_message_header(timestamp, data["sender"])
        body_text = data["body_text"]
        body_entities = list(data["body_entities"])
        is_forwarded = bool(data.get("is_forwarded"))
        if is_forwarded:
            forward_name = data.get("forward_name") or ""
            label = f"Forwarded from {forward_name}" if forward_name else "Forwarded message"
            attribution_text = label
            attribution_entities = []

            if forward_name:
                source_start = u16len("Forwarded from ")
                source_length = u16len(forward_name)
                if source_length:
                    attribution_entities.append(tl.MessageEntityBold(
                        offset=source_start, length=source_length
                    ))

            forward_timestamp = data.get("forward_timestamp")
            if forward_timestamp is not None:
                forward_date = format_archive_timestamp(forward_timestamp, date_only=True)
                if data.get("forward_timestamp_has_time"):
                    time_format = "%H:%M:%S" if data.get("forward_timestamp_has_seconds") else "%H:%M"
                    forward_date += f" · {forward_timestamp:{time_format}}"
                date_start = u16len(attribution_text + "\n")
                attribution_text += "\n" + forward_date
                attribution_entities.append(tl.MessageEntityItalic(
                    offset=date_start, length=u16len(forward_date)
                ))

            try:
                blockquote = tl.MessageEntityBlockquote(
                    offset=0, length=u16len(attribution_text), collapsed=False
                )
            except TypeError:  # compatibility with Telethon versions without `collapsed`
                blockquote = tl.MessageEntityBlockquote(
                    offset=0, length=u16len(attribution_text)
                )
            attribution_entities.append(blockquote)

        reply_to, reply_fallback, deferred = replylinks.resolve_reply(
            data, source_id, id_map, pending_replies, ctx
        )
        if deferred:
            return

        if is_forwarded:
            # Keep sender/date header, quoted forward attribution, and the
            # forwarded text in one destination message (or media caption).
            # The blockquote entity covers only "Forwarded from" and the
            # original source timestamp; payload text follows outside the bar.
            attribution_part = (attribution_text + "\n\n", attribution_entities)
            text, entities = compose_message(
                header, ("", []), attribution_part, body_text, body_entities,
                extra_before_body=reply_fallback,
            )
            text, entities = strip_with_entities(text, entities)
            preview_text, preview_entities = text, entities
        else:
            text, entities = compose_message(
                header, ("", []), ("", []), body_text, body_entities,
                extra_before_body=reply_fallback,
            )
            text, entities = strip_with_entities(text, entities)
            preview_text, preview_entities = text, entities

        oversize = bigfiles.find_oversize_media(data["media"])
        if oversize:
            if config.settings.dry_run:
                bigfiles.print_oversize_dry_run(source_id, data, oversize)
                ctx["oversize_report"].append(source_id)
                mark_completed = True
                return
            bigfiles.halt_for_oversize(source_id, data, text, oversize, id_map, ctx)
            return

        annotation_text, annotation_entities = compose_message(header, ("", []), ("", []), "", [])
        annotation_text, annotation_entities = strip_with_entities(annotation_text, annotation_entities)
        annotation = [(annotation_text, annotation_entities)] if annotation_text.strip() else None

        kinds = [kind for _path, kind in data["media"]]
        # Every supported media type carries its message context as a media
        # caption. Text-only messages use the larger message limit.
        first_limit = (
            config.settings.caption_limit if kinds
            else config.settings.text_limit
        )
        kind_summary = ",".join(kinds) or "text"
        when = timestamp.strftime("%Y-%m-%d %H:%M") if timestamp else "?"

        if config.settings.dry_run:
            if data.get("_inferred_album"):
                print(
                    f"  [dry] album ids={','.join(data['_album_source_ids'])} "
                    f"({len(data['media'])} media items)"
                )
                stats["sent"] += len(data["_album_source_ids"])
                for member_id in data["_album_source_ids"]:
                    pending_replies.pop(member_id, None)
            else:
                _print_dry_run(
                    source_id, data, preview_text, preview_entities, first_limit, reply_to,
                    kind_summary, stats, pending_replies,
                )
            mark_completed = True
            return

        if not await _send_migrated_message(
            client, channel, data, source_id, text, entities, first_limit,
            reply_to, annotation, partial, pending_replies, id_map, stats,
            ctx, kind_summary, when,
        ):
            if data.get("_inferred_album"):
                album.stop_after_failed_album(ctx, source_id)
            return
        mark_completed = True
        await asyncio.sleep(config.settings.send_delay_seconds)
    except Exception as exc:
        mid = ("message" + source_id) if source_id else elem.get("id", "?")
        print(f"    failed {mid}: {type(exc).__name__}: {exc}")
        progress.log_failure(mid, f"{type(exc).__name__}: {exc}")
        stats["failed"] += 1
        if "data" in locals() and data and data.get("_inferred_album"):
            album.stop_after_failed_album(ctx, source_id)
    finally:
        if source_id and mark_completed:
            ctx["processed_source_ids"].add(source_id)
            if "data" in locals() and data and data.get("_inferred_album"):
                ctx["processed_source_ids"].update(data["_album_source_ids"])
        elem.clear()
        while elem.getprevious() is not None:
            del elem.getparent()[0]


async def run_async(settings=None, **overrides):
    """Run (or resume) one migration pass.

    Returns True when the run completed, was aborted by the user at the
    confirmation prompt, or stopped cleanly at an oversize file (all three
    are expected outcomes). Returns False only when the run could not start —
    no export was found in ``base_dir``, or the destination channel could not
    be resolved. Raises :class:`~telegram_migration.config.ConfigError` when
    the credentials or the destination are missing or invalid.
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

    files = htmlexport.discover_export_files(config.settings.base_dir)
    if not files:
        print(f"No messages.html / messages2.html ... found in: {config.settings.base_dir}")
        return False

    id_map = progress.load_id_map()
    manual_done, manual_map = bigfiles.load_manual_uploads()
    for _src, _dst in manual_map.items():
        id_map.setdefault(_src, _dst)
    if manual_done:
        _ids = sorted(manual_done, key=int)
        _shown = ", ".join(_ids[:20]) + (" ..." if len(_ids) > 20 else "")
        print(f"{len(manual_done)} message(s) marked as uploaded by hand and SKIPPED by the script (ids: {_shown}).")
        print(f"  Make sure they are really in the channel; remove a line from {os.path.basename(config.settings.manual_uploads_file)} to have the script send it.")
    partial = progress.load_partial()
    pending_replies = progress.load_pending_replies()
    source_ids = progress.collect_source_ids(files)
    total = len(source_ids)
    reaction_map = reaction_archive.load_json_reactions(files, source_ids)

    print(f"Export files: {len(files)}   Already processed: {len(id_map)}   File(s) contain: {total} messages")
    print(f"{len(pending_replies)} deferred replies are queued.")

    client = TelegramClient(config.settings.session_name, config.settings.api_id, config.settings.api_hash)
    await client.start(bot_token=config.settings.bot_token)
    print("Logged in officially as a bot.")

    try:
        channel = await client.get_entity(config.settings.channel_peer)
    except Exception as e:
        print(f"Channel error: {e}")
        await client.disconnect()
        return False

    if not cli.confirm_target(
            channel, max(0, total - len(set(id_map) | manual_done)), len(files), config.get_settings()):
        print("Aborted by user.")
        await client.disconnect()
        return True

    inline_custom_emoji_ids = await customemoji.prepare_inline_custom_emojis_for_run(client, files, peer=channel, dry_run=config.settings.dry_run)
    stats = {"sent": 0, "skipped": 0, "failed": 0, "forwarded": 0, "reaction_notes": 0}
    ctx = {
        "inline_custom_emoji_ids": inline_custom_emoji_ids,
        "last_sender": "",
        "current_export_date": None,
        "stop": False,
        "source_ids": source_ids,
        "processed_source_ids": set(),
        "reaction_map": reaction_map,
        "reply_map": {},
        "audit": [],
        "manual_done": manual_done,
        "halt": [],
        "oversize_report": [],
        "last_dest_id": None,
        "inferred_groups": htmlexport.infer_media_groups(files),
    }

    for path in files:
        if await process_file(client, channel, path, id_map, partial, pending_replies, stats, ctx):
            break

    if not ctx["stop"] and pending_replies:
        while pending_replies and not ctx["stop"]:
            before_pending = len(pending_replies)
            before_completed = len(ctx["processed_source_ids"])
            pending_ctx = dict(ctx)
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
        progress.save_pending_replies(pending_replies)
        print(f"Stopped at message id={ctx['halt'][0]}: sent={stats['sent']} this run, progress saved.")
        print("Follow the instructions above, then run the script again to resume.")
        await client.disconnect()
        return True

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
    return True
