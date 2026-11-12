"""Helpers for inferred media albums reconstructed from Telegram Desktop HTML."""

from copy import copy

from telethon.errors import MessageNotModifiedError
from telethon.tl import types as tl

from . import exportmedia
from .albumcaption import caption_for_group, caption_for_group_body
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

    data["_inferred_album"] = True
    data["_album_source_ids"] = member_ids
    data["_album_caption_text"] = album_caption
    data["_album_caption_entities"] = album_caption_entities
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
    if data.get("is_forwarded") and not data.get("forward_name"):
        return False
    return True


def _caption_signature(record):
    """Normalize a visible source caption for album matching."""
    import re
    return re.sub(r"\s+", " ", (record.get("body_text") or "")).strip()


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
    if left_caption and right_caption and not same_caption and not joined_continuation:
        return False

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
    last_sender = ""
    forward_context = {
        "last_forward_name": "",
        "last_forward_timestamp": None,
        "last_forward_timestamp_has_time": False,
        "last_forward_timestamp_has_seconds": False,
    }
    for page in files:
        pending = []

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
                    htmlexport.reset_forwarded_context(forward_context)
                    continue

                data = htmlexport.extract_message_data(elem)
                if data is None:
                    flush()
                    last_sender = ""
                    htmlexport.reset_forwarded_context(forward_context)
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
                    elif _album_records_compatible(pending[-1], data):
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

    source_ids = data["_album_source_ids"]
    state = dict(partial.get(str(source_id), {}) or {})
    saved_ids = state.get("album_message_ids")
    mapped_ids = [int(id_map[member_id]) for member_id in source_ids]
    if saved_ids is not None and [int(value) for value in saved_ids] != mapped_ids:
        raise RuntimeError(
            f"Partial album IDs disagree with the migration ID map for source id={source_id}; "
            "refusing to guess which destination messages belong to this album"
        )
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
    await sending.send_inferred_album_caption(
        client, channel, data.get("_album_caption_text", ""),
        data.get("_album_caption_entities", []), state, source_id, mapped_ids[-1],
    )


async def _album_message_list(value):
    """Normalize Telethon's single/list return shapes to a list of messages."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


async def _verify_album_delivery(client, channel, messages, captions,
                                 caption_entities, source_id):
    """Verify Telegram created one album and retained every supplied caption.

    A successful send_file call and the expected number of returned IDs are not
    enough: Telegram may split the media group, or a caption may be lost.
    Fail closed rather than mapping the source records as a faithful album.
    """
    if len(messages) != len(captions):
        raise RuntimeError(
            f"Telegram returned {len(messages)} messages for source album "
            f"id={source_id}; expected {len(captions)}"
        )

    grouped_ids = [getattr(message, "grouped_id", None) for message in messages]
    if any(group_id is None for group_id in grouped_ids) or len(set(grouped_ids)) != 1:
        raise RuntimeError(
            f"Telegram did not confirm one grouped album for source id={source_id}; "
            f"returned grouped_id values={grouped_ids!r}. The source album has not "
            "been marked complete."
        )

    for index, (message, expected, entities) in enumerate(
        zip(messages, captions, caption_entities), start=1
    ):
        actual = getattr(message, "message", None)
        if actual is None:
            actual = getattr(message, "text", None)
        if actual is None:
            raise RuntimeError(
                f"Cannot verify caption for album item {index} "
                f"(source id={source_id}); Telegram returned no message text field"
            )
        if actual == expected:
            continue

        # Caption text is content, not decoration. If Telegram omitted or
        # altered it on upload, try to restore it on the already-sent message.
        edit_message = getattr(client, "edit_message", None)
        if not callable(edit_message):
            raise RuntimeError(
                f"Telegram did not retain caption for album item {index} "
                f"(source id={source_id}); no edit_message method is available"
            )
        try:
            edited = await edit_message(
                channel, message.id, expected,
                formatting_entities=entities or None, parse_mode=None,
            )
        except MessageNotModifiedError:
            # This is a positive server response: Telegram rejected the edit
            # specifically because the requested caption and formatting were
            # already present. The locally re-fetched TL object can encode
            # optional flags differently (notably blockquote.collapsed), so
            # comparing its raw entities would turn a confirmed no-op into a
            # false failure and cause the whole album to be retried.
            continue

        edited_text = getattr(edited, "message", None) if edited is not None else None
        if edited_text is None:
            get_messages = getattr(client, "get_messages", None)
            if callable(get_messages):
                refreshed = await get_messages(channel, ids=message.id)
                refreshed_list = await _album_message_list(refreshed)
                if refreshed_list:
                    edited_text = getattr(refreshed_list[0], "message", None)
        if edited_text != expected:
            raise RuntimeError(
                f"Caption verification failed for album item {index} "
                f"(source id={source_id}); expected text was not retained"
            )

    return grouped_ids[0]


async def _verify_saved_album(client, channel, saved_ids, captions,
                              caption_entities, source_id):
    """Fetch and verify an album recorded by an earlier interrupted run."""
    get_messages = getattr(client, "get_messages", None)
    if not callable(get_messages):
        raise RuntimeError(
            f"Cannot verify previously sent album for source id={source_id}; "
            "the Telegram client cannot fetch saved message IDs"
        )
    saved_messages = await get_messages(channel, ids=saved_ids)
    messages = await _album_message_list(saved_messages)
    if len(messages) != len(saved_ids) or any(message is None for message in messages):
        raise RuntimeError(
            f"Could not retrieve every saved message for source album id={source_id}; "
            "refusing to mark the album complete or resend it automatically"
        )
    return await _verify_album_delivery(
        client, channel, messages, captions, caption_entities, source_id
    )
