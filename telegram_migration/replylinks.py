"""Verify that migrated replies point at the right destination messages."""

import asyncio

from . import config


def _reply_id_from_message(msg):
    reply_header = getattr(msg, "reply_to", None) if msg is not None else None
    actual = getattr(reply_header, "reply_to_msg_id", None) if reply_header is not None else None
    if actual is None and msg is not None:
        actual = getattr(msg, "reply_to_msg_id", None)
    return actual


async def verify_all_reply_links(client, channel, source_reply_map, id_map, audit):
    """Verify every available source reply, batching message reads. Results go into `audit`."""
    if not config.settings.verify_replies or not source_reply_map:
        return

    candidates = []
    for source_id, source_target in source_reply_map.items():
        source_id, source_target = str(source_id), str(source_target)
        if source_id not in id_map:
            continue
        expected = id_map.get(source_target)
        if expected is None:
            audit.append({
                "src": source_id,
                "source_reply_to": source_target,
                "dst": int(id_map[source_id]),
                "verified": False,
                "status": "target_not_migrated",
            })
        else:
            candidates.append((source_id, source_target, int(id_map[source_id]), int(expected)))

    print(f"Verifying reply links for {len(candidates)} migrated source message(s)...")
    # Telegram's GetMessages request accepts multiple IDs; use batches to avoid
    # making one network round-trip per reply in a large archive.
    batch_size = 100
    for offset in range(0, len(candidates), batch_size):
        batch = candidates[offset:offset + batch_size]
        dest_ids = [row[2] for row in batch]
        try:
            received = await client.get_messages(channel, ids=dest_ids)
            if received is None:
                received = []
            elif not isinstance(received, (list, tuple)):
                received = [received]
            by_id = {int(m.id): m for m in received if m is not None and getattr(m, "id", None) is not None}
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            for source_id, source_target, dest_id, expected in batch:
                audit.append({
                    "src": source_id,
                    "source_reply_to": source_target,
                    "dst": dest_id,
                    "expected_destination_reply_to": expected,
                    "verified": False,
                    "status": "verification_unavailable",
                    "error": reason,
                })
            print(f"  Warning: could not verify a batch of {len(batch)} replies: {reason}")
            continue

        for source_id, source_target, dest_id, expected in batch:
            msg = by_id.get(dest_id)
            actual = _reply_id_from_message(msg)
            verified = actual == expected
            audit.append({
                "src": source_id,
                "source_reply_to": source_target,
                "dst": dest_id,
                "expected_destination_reply_to": expected,
                "actual_destination_reply_to": actual,
                "verified": verified,
                "status": "verified" if verified else ("message_not_found" if msg is None else "mismatch"),
            })
            if not verified:
                print(f"  WARNING: reply id={source_id} expected destination target {expected}, got {actual}")
        await asyncio.sleep(0.1)
