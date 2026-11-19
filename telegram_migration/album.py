"""Helpers for inferred media albums reconstructed from Telegram Desktop HTML."""

from copy import copy

from telethon.tl import types as tl

from . import exportmedia
from .albumcaption import caption_for_group, caption_for_group_body
from .albumverification import _album_message_list, _verify_album_delivery, _verify_saved_album
from .constants import STICKER_KINDS
from .htmlutil import css_classes

from .textutil import (
    compose_message,
    compose_message_header,
    format_archive_timestamp,
    strip_with_entities,
    u16len,
)


def caption_for_record(record):
    """Build the original archive header and forward attribution for one album item."""
    timestamp = record.get("timestamp")
    if record.get("service_event"):
        header = compose_message_header(
            timestamp, "Telegram service event", date_only=True
        )
    else:
        header = compose_message_header(timestamp, record.get("sender", ""))

    if record.get("is_forwarded"):
        forward_name = record.get("forward_name") or ""
        label = f"Forwarded from {forward_name}" if forward_name else "Forwarded message"
        attribution = label
        entities = []
        if forward_name:
            entities.append(tl.MessageEntityBold(
                offset=u16len("Forwarded from "),
                length=u16len(forward_name),
            ))

        forward_timestamp = record.get("forward_timestamp")
        if forward_timestamp is not None:
            forward_date = format_archive_timestamp(forward_timestamp, date_only=True)
            if record.get("forward_timestamp_has_time"):
                time_format = "%H:%M:%S" if record.get("forward_timestamp_has_seconds") else "%H:%M"
                forward_date += f" · {forward_timestamp:{time_format}}"
            entities.append(tl.MessageEntityItalic(
                offset=u16len(attribution + "\n"),
                length=u16len(forward_date),
            ))
            attribution += "\n" + forward_date

        try:
            quote = tl.MessageEntityBlockquote(
                offset=0, length=u16len(attribution), collapsed=False
            )
        except TypeError:
            quote = tl.MessageEntityBlockquote(offset=0, length=u16len(attribution))
        entities.append(quote)
        text, ents = compose_message(
            header, ("", []), (attribution + "\n\n", entities),
            record.get("body_text", ""), record.get("body_entities", []),
            
        )
    else:
        text, ents = compose_message(
            header, ("", []), ("", []),
            record.get("body_text", ""), record.get("body_entities", []),
            
        )
    return strip_with_entities(text, ents)


def prepare_inferred_group(data, group, id_map, manual_done, partial, persist_id):
    """Attach an attributed album payload and recover mappings after interruption."""
    if not group or len(group) < 2:
        return False

    member_ids = [str(member["msg_id"]) for member in group]
    saved_state = partial.get(str(data["msg_id"]), {})
    saved_album_ids = saved_state.get("album_message_ids")
    if saved_album_ids and len(saved_album_ids) == len(member_ids):
        for member_id, destination_id in zip(member_ids, saved_album_ids):
            if member_id not in id_map:
                id_map[member_id] = int(destination_id)
                persist_id(member_id, destination_id)

    already_mapped = [
        member_id for member_id in member_ids
        if member_id in id_map or member_id in manual_done
    ]
    if already_mapped and len(already_mapped) != len(member_ids):
        return False

    album_caption, album_caption_entities = caption_for_group(group)
    legacy_caption, legacy_caption_entities = caption_for_group_body(group)
    all_media = [item for member in group for item in member["media"]]

    # When Telegram cannot represent this inferred source group as one native
    # media album, keep every original message's own caption/author/time for
    # the individual-upload fallback. Do not change captions for native albums.
    if not exportmedia.album_compatible(all_media):
        individual_captions = [caption_for_record(member) for member in group]
        if len(individual_captions) != len(all_media):
            raise RuntimeError(
                "Inferred mixed-media group has a different number of source "
                "records and media items; refusing to lose per-message attribution"
            )
        data["_individual_media_captions"] = individual_captions

    # Consecutive audio records are separate source messages, even when the
    # destination supports sending them together as one media group. Preserve
    # each record's own sender/date and body beside its corresponding audio.
    audio_item_captions = None
    if all(
        len(member.get("media") or []) == 1
        and member["media"][0][1] == "audio"
        for member in group
    ):
        audio_item_captions = [caption_for_record(member) for member in group]

    data["_inferred_album"] = True
    data["_album_source_ids"] = member_ids
    data["_album_caption_text"] = album_caption
    data["_album_caption_entities"] = album_caption_entities
    if audio_item_captions is not None:
        data["_album_item_captions"] = audio_item_captions
    data["_album_caption_legacy_text"] = legacy_caption
    data["_album_caption_legacy_entities"] = legacy_caption_entities
    data["media"] = all_media
    data["body_text"] = ""
    data["body_entities"] = []
    # Never let a native-forward shortcut bypass reconstruction of the full group.
    data["forward_source"] = None
    # Keep the first record's sender, forwarded attribution, and reply target.
    return True
def _album_candidate(data, effective_sender):
    """Whether one HTML record can start or continue a conservative album inference.

    The first member of an album is often not marked "joined" in Telegram
    Desktop HTML. Continuation markers and timestamps are evaluated between
    adjacent candidates in _album_records_compatible().
    """
    if len(data.get("media") or []) != 1:
        return False
    if data.get("media_note"):
        return False
    # A round-video note is its own Telegram message type, not an album
    # continuation. Telegram Desktop may still apply the visual "joined" class
    # to adjacent notes, so never infer a source album from that layout marker.
    if data["media"][0][1] == "round_video":
        return False
    # Consecutive audio tracks and voice notes are separate source messages.
    # Keep each one's sender, timestamp, and text in that same media caption.
    if data["media"][0][1] in {"audio", "voice"}:
        return False
    # Stickers are standalone Telegram messages, not inferred media albums.
    # Every sticker keeps its own sender/date in its own media caption.
    media_path, media_kind = data["media"][0]
    if media_kind in STICKER_KINDS:
        return False
    if data.get("is_forwarded") and not data.get("forward_name"):
        return False
    return True


def _caption_signature(record):
    """Normalize a visible source caption for album matching."""
    import re
    return re.sub(r"\s+", " ", (record.get("body_text") or "")).strip()


def _caption_conflicts_with_group(group, candidate):
    """Whether a candidate adds a different caption to the pending source run.

    Compare against every caption already in the run, not only the immediately
    preceding record. This catches A -> no caption -> B as a hard boundary.
    """
    candidate_caption = _caption_signature(candidate)
    if not candidate_caption:
        return False
    return any(
        existing_caption and existing_caption != candidate_caption
        for existing_caption in (_caption_signature(record) for record in group)
    )


def _album_records_compatible(left, right):
    """Check whether adjacent records share a plausible album context.

    Identical non-empty captions are strong evidence that separate HTML
    records are members of one Telegram media group. Telegram Desktop may
    attach a single album caption to only one member, so a one-sided caption
    is also accepted when the surrounding sender/forward context and source
    timestamps support a short consecutive sequence.
    """
    if left.get("_album_sender") != right.get("_album_sender"):
        return False
    if left.get("is_forwarded") != right.get("is_forwarded"):
        return False
    if left.get("forward_name", "") != right.get("forward_name", ""):
        return False
    # Telegram Desktop can repeat the same reply preview on every forwarded
    # continuation in an album. That repeated target is shared album context,
    # not a boundary. A different reply target on a later item is a boundary.
    left_reply = left.get("forward_reply_src_id") if left.get("is_forwarded") else left.get("reply_src_id")
    right_reply = right.get("forward_reply_src_id") if right.get("is_forwarded") else right.get("reply_src_id")
    if right_reply and right_reply != left_reply:
        return False

    left_caption = _caption_signature(left)
    right_caption = _caption_signature(right)
    same_caption = bool(left_caption and right_caption and left_caption == right_caption)
    # Different non-empty captions are always separate source-message
    # boundaries. Telegram Desktop's generic "joined" layout marker must
    # never override an explicit caption difference.
    if left_caption and right_caption and not same_caption:
        return False

    left_time, right_time = left.get("timestamp"), right.get("timestamp")
    outer_gap = None
    if left_time is not None and right_time is not None:
        outer_gap = (right_time - left_time).total_seconds()
        if outer_gap < 0:
            return False

    # The joined class is Telegram Desktop's explicit continuation marker.
    # Do not discard that structural evidence because clocks are missing or
    # differ by more than an arbitrary number of seconds.
    joined_continuation = bool(right.get("joined"))
    left_forward_time = left.get("forward_timestamp")
    right_forward_time = right.get("forward_timestamp")
    if left.get("is_forwarded"):
        # A joined class describes Desktop's visual message layout; it does
        # not prove that two forwarded messages belong to one media group.
        # Require each forwarded item to carry its own original-source
        # timestamp before using timestamp proximity as album evidence.
        if left_forward_time is None or right_forward_time is None:
            return False
        forward_gap = (right_forward_time - left_forward_time).total_seconds()
        if forward_gap < 0 or (forward_gap > 30 and not same_caption):
            return False
    elif not (same_caption or joined_continuation):
        # For ordinary messages, a joined continuation plus a short export-time
        # gap remains the fallback when no stronger caption evidence exists.
        if outer_gap is None or outer_gap > 30:
            return False

    # Source grouping is deliberately independent of destination album
    # support. The sender decides whether a confirmed source run can be sent
    # natively or must be preserved as ordered individual messages.
    return True

def infer_media_groups(files, inline_emoji_ids=None):
    """Infer likely media groups from consecutive media records in Desktop HTML.

    Desktop HTML does not retain Telegram's grouped_id. Album starts may lack
    the "joined" class, so single-media records are held as possible starts.
    Group membership is inferred from the sender/forward context, captions,
    joined layout markers, and source timestamps. A missing "joined" class
    does not make a record a standalone prelude. Compatible consecutive runs
    are emitted in source order, in batches of at most Telegram's ten-item
    album limit; any remainder of 2–10 records becomes the next group.
    """
    from lxml import etree

    from . import htmlexport

    groups = {}
    for page in files:
        pending = []
        last_sender = ""
        forward_context = {"last_forward_name": ""}

        def flush():
            nonlocal pending
            if 2 <= len(pending) <= 10:
                groups[pending[0]["msg_id"]] = pending[:]
            pending = []

        with open(page, "rb") as export_file:
            context = etree.iterparse(
                export_file, events=("end",), tag="div", html=True, encoding="utf-8"
            )
            for _event, elem in context:
                if "message" not in css_classes(elem):
                    continue

                classes = css_classes(elem)
                if "service" in classes:
                    flush()
                    last_sender = ""
                    forward_context["last_forward_name"] = ""
                    continue

                data = htmlexport.extract_message_data(elem)
                if data is None:
                    flush()
                    last_sender = ""
                    forward_context["last_forward_name"] = ""
                    continue

                htmlexport.inherit_forwarded_sender(data, forward_context)
                if inline_emoji_ids:
                    from .customemoji import apply_custom_emoji_entities
                    apply_custom_emoji_entities(data, inline_emoji_ids)

                if data.get("sender"):
                    last_sender = data["sender"]
                effective_sender = data.get("sender") or (
                    last_sender if data.get("joined") else ""
                )
                if not _album_candidate(data, effective_sender):
                    flush()
                else:
                    data["sender"] = effective_sender
                    # For forwarded posts, the original forwarded author is
                    # the stable group identity. The outer forwarder may only
                    # be rendered on some joined rows in Desktop HTML.
                    data["_album_sender"] = (
                        data.get("forward_name") if data.get("is_forwarded")
                        else effective_sender
                    ) or effective_sender
                    last_sender = effective_sender
                    if not pending:
                        pending = [data]
                    elif (
                        _album_records_compatible(pending[-1], data)
                        and not _caption_conflicts_with_group(pending, data)
                    ):
                        pending.append(data)
                        # Preserve source order when a compatible run exceeds
                        # Telegram's ten-item album limit. The first ten stay
                        # together; the remainder starts the next batch.
                        if len(pending) == 11:
                            album_run = pending[:10]
                            groups[album_run[0]["msg_id"]] = album_run
                            pending = pending[10:]
                    else:
                        flush()
                        pending = [data]

                elem.clear()
                while elem.getprevious() is not None:
                    del elem.getparent()[0]

        flush()

    return groups


def stop_after_failed_album(ctx, source_id):
    """Stop before later records when an inferred album could not be sent."""
    ctx["stop"] = True
    print(
        f"  Stopped at failed inferred album beginning with id={source_id}. "
        "Later messages were left untouched; restart to retry this album."
    )


def map_destination_ids(source_ids, destination_ids, id_map, persist_id):
    """Persist one destination message ID for every original album source record."""
    if len(source_ids) != len(destination_ids):
        raise RuntimeError(
            f"Inferred album returned {len(destination_ids)} destination IDs "
            f"for {len(source_ids)} source messages"
        )
    for source_id, destination_id in zip(source_ids, destination_ids):
        id_map[str(source_id)] = int(destination_id)
        persist_id(source_id, destination_id)
    return int(destination_ids[0])


async def verify_mapped_album(client, channel, data, id_map, partial, source_id):
    """Audit a mapped album before skipping it; never resend media automatically."""
    from . import progress, sending
    from .caption_sending import (
        inferred_album_item_caption_parts,
        send_inferred_album_item_caption_continuations,
    )

    source_ids = data["_album_source_ids"]
    state = dict(partial.get(str(source_id), {}) or {})
    saved_ids = state.get("album_message_ids")
    mapped_ids = [int(id_map[member_id]) for member_id in source_ids]
    if saved_ids is not None and [int(value) for value in saved_ids] != mapped_ids:
        raise RuntimeError(
            f"Partial album IDs disagree with the migration ID map for source id={source_id}; "
            "refusing to guess which destination messages belong to this album"
        )
    if data.get("_album_item_captions") is not None:
        expected_captions, expected_entities, _continuations = (
            inferred_album_item_caption_parts(data["_album_item_captions"])
        )
    else:
        first_caption, _continuation = sending.inferred_album_caption_parts(
            data.get("_album_caption_text", ""),
            data.get("_album_caption_entities", []),
        )
        expected_captions = [""] * (len(mapped_ids) - 1) + [first_caption[0]]
        expected_entities = [[] for _ in mapped_ids[:-1]] + [first_caption[1]]
    grouped_id = await _verify_saved_album(
        client, channel, mapped_ids, expected_captions, expected_entities, source_id
    )
    state["src"] = str(source_id)
    state["album_id"] = mapped_ids[0]
    state["album_message_ids"] = mapped_ids
    state["album_grouped_id"] = int(grouped_id)
    progress.save_state(source_id, state)
    print(
        f"    audited existing Telegram album for source id={source_id}: "
        f"{len(mapped_ids)} items, grouped_id={grouped_id}"
    )
    if data.get("_album_item_captions") is not None:
        await send_inferred_album_item_caption_continuations(
            client, channel, data["_album_item_captions"], state, source_id,
            mapped_ids,
        )
    else:
        await sending.send_inferred_album_caption(
            client, channel, data.get("_album_caption_text", ""),
            data.get("_album_caption_entities", []), state, source_id, mapped_ids[-1],
        )

