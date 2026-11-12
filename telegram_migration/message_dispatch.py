"""Prepare and record individual migrated messages."""

from . import album, config, progress, replylinks, sending
from .textutil import split_with_entities


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


