"""Sending one migrated message: text chunks, albums, voice/stickers, native forwards."""

import asyncio
import os

from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl import types as tl

from . import album, config, exportmedia, formatfallback, progress, uploads
from .textutil import clean_invalid_entities, split_with_entities
from .constants import STICKER_KINDS


_native_warned = [False]


async def resolve_mentions(client, entities):
    """Bots usually cannot resolve arbitrary users; such mentions stay plain text."""
    cls_in = getattr(tl, "InputMessageEntityMentionName", None)
    cls_out = getattr(tl, "MessageEntityMentionName", None)
    if cls_out is None:
        return entities
    out = []
    for e in entities:
        if isinstance(e, cls_out):
            if cls_in is None:
                continue
            try:
                inp = await client.get_input_entity(e.user_id)
                out.append(cls_in(e.offset, e.length, inp))
            except FloodWaitError:
                raise
            except Exception:
                pass
        else:
            out.append(e)
    return out


async def _try_native_forward(client, channel, peer_hint, source_msg_id,
                               archive_source_id=None):
    try:
        pid = int(peer_hint) if str(peer_hint).lstrip("-").isdigit() else peer_hint
        peer = await client.get_entity(pid)
        result = await client.forward_messages(channel, source_msg_id, from_peer=peer)
        msg = result[0] if isinstance(result, (list, tuple)) else result
        return msg.id
    except FloodWaitError:
        raise
    except Exception as exc:
        if not _native_warned[0]:
            _native_warned[0] = True
            print(
                f"    native forward not possible for archive source id={archive_source_id} "
                f"(peer={peer_hint!r}, message_id={source_msg_id}; "
                f"{type(exc).__name__}: {exc}); using the textual header "
                "(further notices suppressed)"
            )
        return None


async def _send_text_chunks(client, channel, chunks, reply_to, state, prog_key,
                            source_id=None, persist=True):
    """Send each text chunk in order, remembering progress.

    Takes the whole `state` so each successful chunk can be checkpointed to
    disk (when `persist` is true). A crash between chunks then resumes at the
    next unsent chunk rather than replaying from the top. Even a single-chunk
    send benefits from the checkpoint: if the process dies between Telegram
    confirming the message and the id-map write, the persisted `done` count
    prevents the next run from repeating it.
    """
    prog = state.setdefault(prog_key, {})
    done = prog.get("done", 0)
    if done > len(chunks):
        print(f"    note: resetting stale {prog_key}.done={done} for src={state.get('src')} (current chunk count {len(chunks)})")
        prog.clear()
    for i, (text, ents) in enumerate(chunks):
        if i < prog.get("done", 0):
            continue
        ents = clean_invalid_entities(text, ents)
        m = await formatfallback.with_formatting_fallback(
            lambda **kw: client.send_message(channel, text, **kw),
            {"formatting_entities": ents or None,
             "reply_to": prog.get("last", reply_to),
             "parse_mode": None},
            text, ents, source_id,
        )
        prog.setdefault("first", m.id)
        prog["last"] = m.id
        prog["done"] = i + 1
        src = state.get("src")
        if persist and src is not None:
            progress.save_state(src, state)
    return prog["first"]


def _message_text(message):
    if message is None:
        return None
    value = getattr(message, "message", None)
    return value if value is not None else getattr(message, "text", None)


async def _get_message_by_id(client, channel, message_id):
    getter = getattr(client, "get_messages", None)
    if not callable(getter):
        return None
    message = await getter(channel, ids=int(message_id))
    if isinstance(message, (list, tuple)):
        return message[0] if message else None
    return message


async def _ensure_media_caption(client, channel, message, caption, entities, source_id):
    """Confirm Telegram retained a media caption; repair it on the same message."""
    if not caption or _message_text(message) == caption:
        return message

    message_id = getattr(message, "id", None)
    edit = getattr(client, "edit_message", None)
    if message_id is None or not callable(edit):
        raise RuntimeError(
            f"Telegram returned media without the caption for source id={source_id}; "
            "the caption cannot be repaired in place"
        )

    try:
        edited = await formatfallback.with_formatting_fallback(
            lambda **kw: edit(channel, int(message_id), caption, **kw),
            {"formatting_entities": entities or None, "parse_mode": None},
            caption, entities, source_id,
        )
    except FloodWaitError:
        raise
    except MessageNotModifiedError:
        edited = None
    except Exception as exc:
        raise RuntimeError(
            f"Telegram did not retain the media caption for source id={source_id} "
            f"(message {message_id}); in-place repair failed: {type(exc).__name__}: {exc}"
        ) from exc

    if _message_text(edited) == caption:
        return edited
    refreshed = await _get_message_by_id(client, channel, message_id)
    if _message_text(refreshed) == caption:
        return refreshed
    raise RuntimeError(
        f"Telegram did not retain the media caption for source id={source_id} "
        f"(message {message_id}); refusing to mark a senderless media post complete"
    )


async def _send_native_forward(client, channel, forward_source, reply_to, state,
                               annotation, source_id):
    if (forward_source is not None and config.settings.native_forward
            and reply_to is None and not state.get("force_reconstruction")
            and not annotation):
        new_id = state.get("native_id")
        if new_id is None and not state.get("fwd_tried"):
            peer_hint, src_id = forward_source
            if peer_hint != "__text__" and isinstance(src_id, int):
                new_id = await _try_native_forward(
                    client, channel, peer_hint, src_id, source_id
                )
            if new_id is not None:
                state["native"] = True
                state["native_id"] = int(new_id)
                progress.save_state(state["src"], state)
            else:
                state["fwd_tried"] = True
                progress.save_state(state["src"], state)
        if new_id is not None:
            state["native"] = True
            state["native_id"] = int(new_id)
            if config.settings.forward_annotate and annotation:
                await _send_text_chunks(client, channel, annotation, new_id,
                                        state, "ann_prog", source_id)
            return new_id
    return None


def inferred_album_caption_parts(caption, entities):
    """Return the native album caption and any text-limit continuation chunks."""
    if not caption or not caption.strip():
        return ("", []), []
    chunks = split_with_entities(
        caption, entities or [],
        config.settings.caption_limit, config.settings.text_limit,
    )
    return chunks[0], chunks[1:]


async def send_inferred_album_caption(
    client, channel, caption, entities, state, source_id, album_message_id,
):
    """Send only caption overflow as a reply to the final item in the album."""
    _first, continuation = inferred_album_caption_parts(caption, entities)
    if not continuation:
        return
    await _send_text_chunks(
        client, channel, continuation, album_message_id, state,
        "album_caption_continuation_prog", source_id,
    )


async def _send_album(client, channel, chunks, media, reply_to, state, source_id,
                      *, force_document=False, captions=None, caption_entities=None,
                      album_caption=None, album_caption_entities=None,
                      return_all=False):
    first_text, first_ents = chunks[0]
    files = [path for path, _kind in media]
    inferred_album = album_caption is not None
    if inferred_album:
        # Put the complete message context on the final item of the group.
        # Telegram accepts one caption per album item; all earlier slots stay
        # empty so the caption follows the entire media sequence.
        final_caption, _continuation = inferred_album_caption_parts(
            album_caption, album_caption_entities,
        )
        captions = [""] * (len(files) - 1) + [final_caption[0]]
        caption_entities = [[] for _ in files[:-1]] + [final_caption[1]]
    else:
        captions = ([""] * (len(files) - 1) + [first_text]
                    if captions is None else list(captions))
        caption_entities = ([[] for _ in files[:-1]] + [first_ents]
                            if caption_entities is None else list(caption_entities))
    if len(captions) != len(files):
        raise RuntimeError(
            f"Album caption count {len(captions)} does not match media count {len(files)}"
        )
    if len(caption_entities) != len(files):
        raise RuntimeError(
            f"Album entity-row count {len(caption_entities)} does not match media count {len(files)}"
        )

    safe_entity_rows = [
        clean_invalid_entities(caption, entities)
        for caption, entities in zip(captions, caption_entities)
    ]

    async def send_group_caption(album_message_id):
        if inferred_album:
            await send_inferred_album_caption(
                client, channel, album_caption, album_caption_entities,
                state, source_id, album_message_id,
            )

    if "album_message_ids" in state:
        saved_ids = list(state["album_message_ids"])
        if len(saved_ids) != len(files):
            raise RuntimeError(
                f"Saved album state for source id={source_id} contains "
                f"{len(saved_ids)} message IDs for {len(files)} media files; "
                "refusing to silently skip or duplicate media"
            )
        grouped_id = await album._verify_saved_album(
            client, channel, saved_ids, captions, safe_entity_rows, source_id
        )
        state["album_grouped_id"] = int(grouped_id)
        progress.save_state(state["src"], state)
        await send_group_caption(saved_ids[-1])
        return saved_ids if return_all else saved_ids[0]
    if "album_id" in state:
        # Older partial-state files may contain only the first ID. Guessing
        # the remaining IDs is unsafe; never treat such an incomplete record
        # as a successfully migrated album.
        raise RuntimeError(
            f"Legacy partial state for source album id={source_id} has only "
            "album_id and no complete album_message_ids; manual review is required"
        )

    album_kwargs = {"caption": captions, "reply_to": reply_to, "parse_mode": None}
    if force_document:
        album_kwargs["force_document"] = True
    if any(safe_entity_rows):
        # Telethon expects one entity list per caption. Empty lists (rather than
        # None) preserve the list-of-lists shape for captions without entities.
        album_kwargs["formatting_entities"] = safe_entity_rows

    msgs = await formatfallback.with_formatting_fallback(
        lambda **kw: uploads.send_file(client, channel, files, **kw),
        album_kwargs, captions[-1], safe_entity_rows[-1], source_id,
    )
    if not msgs:
        raise RuntimeError("Album upload returned no messages")
    message_list = list(msgs) if isinstance(msgs, (list, tuple)) else [msgs]
    message_ids = [int(message.id) for message in message_list]

    # Save every ID Telegram did return before validating the response. If
    # Telegram accepted only part of the send, a retry must not blindly upload
    # the same media again and create duplicates.
    if message_ids:
        state["album_id"] = message_ids[0]
        state["album_message_ids"] = message_ids
        state.pop("album_grouped_id", None)
        progress.save_state(state["src"], state)

    if len(message_list) != len(files):
        raise RuntimeError(
            f"Album upload returned {len(message_list)} message(s) for "
            f"{len(files)} media items; returned IDs were checkpointed and "
            "the source album was not marked complete"
        )

    grouped_id = await album._verify_album_delivery(
        client, channel, message_list, captions, safe_entity_rows, source_id
    )
    state["album_grouped_id"] = int(grouped_id)
    progress.save_state(state["src"], state)
    print(
        f"    verified Telegram album for source id={source_id}: "
        f"{len(message_ids)} items, grouped_id={grouped_id}"
    )
    await send_group_caption(message_ids[-1])
    return message_ids if return_all else message_ids[0]

async def _send_individual_media(client, channel, chunks, media, reply_to, state,
                                 source_id):
    """Send an ungroupable sequence, then verify its final media caption in place."""
    first_text, first_ents = chunks[0]
    state.setdefault("multi_media_ids", [])
    final_message = None
    while len(state["multi_media_ids"]) < len(media):
        index = len(state["multi_media_ids"])
        path, kind = media[index]
        is_final_item = index == len(media) - 1
        caption = first_text if is_final_item else ""
        entities = first_ents if caption else None
        sent_msg = await uploads.send_media_item(
            client, channel, path, kind, reply_to, source_id,
            caption=caption, entities=entities,
        )
        state["multi_media_ids"].append(sent_msg.id)
        progress.save_state(state["src"], state)
        if is_final_item:
            final_message = sent_msg

    if first_text and not state.get("multi_caption_verified"):
        if final_message is None:
            final_message = await _get_message_by_id(
                client, channel, state["multi_media_ids"][-1]
            )
        if final_message is None:
            raise RuntimeError(
                f"Cannot verify the final media caption for source id={source_id}; "
                "Telegram did not return the saved media message"
            )
        await _ensure_media_caption(
            client, channel, final_message, first_text, first_ents, source_id
        )
        state["multi_caption_verified"] = True
        progress.save_state(state["src"], state)

    state["album_id"] = state["multi_media_ids"][0]
    progress.save_state(state["src"], state)
    return False


async def _send_multiple_media(client, channel, chunks, media, reply_to, state, source_id):
    has_sticker = any(kind in STICKER_KINDS for _path, kind in media)
    individual = (
        has_sticker
        or any(kind in ("gif", "voice", "round_video") for _path, kind in media)
        or not exportmedia.album_compatible(media)
    )
    if individual:
        await _send_individual_media(
            client, channel, chunks, media, reply_to, state, source_id
        )
    else:
        await _send_album(client, channel, chunks, media, reply_to, state, source_id)

    rest = chunks[1:]
    if rest:
        group_ids = (
            state.get("album_message_ids")
            or state.get("multi_media_ids")
            or [state["album_id"]]
        )
        await _send_text_chunks(
            client, channel, rest, group_ids[-1], state, "tail_prog", source_id
        )
    return state["album_id"]


async def _send_text_only(client, channel, chunks, reply_to, state, source_id):
    # persist=True for every chunk count. The default used to be persist=len(chunks) > 1
    # here, on the theory that a single-chunk send has nothing to resume in the middle
    # of — but it also meant a crash between Telegram confirming the message and the
    # id-map write left no recovery point at all, so the next run would repeat that
    # one message. Media sends already save_state immediately after upload; this
    # brings plain text in line with the media path.
    return await _send_text_chunks(
        client, channel, chunks, reply_to, state, "text_prog", source_id,
    )


async def _send_single_media(client, channel, chunks, media, reply_to, state, source_id):
    first_text, first_ents = chunks[0]
    rest = chunks[1:]
    path, kind = media[0]
    media_msg = None
    if "media_id" not in state:
        media_msg = await uploads.send_media_item(
            client, channel, path, kind, reply_to, source_id,
            caption=first_text, entities=first_ents,
        )
        state["media_id"] = media_msg.id
        progress.save_state(state["src"], state)
    if first_text and not state.get("media_caption_verified"):
        if media_msg is None:
            media_msg = await _get_message_by_id(client, channel, state["media_id"])
        if media_msg is None:
            raise RuntimeError(
                f"Cannot verify the media caption for source id={source_id}; "
                "Telegram did not return the saved media message"
            )
        await _ensure_media_caption(
            client, channel, media_msg, first_text, first_ents, source_id
        )
        state["media_caption_verified"] = True
        progress.save_state(state["src"], state)
    if rest:
        await _send_text_chunks(client, channel, rest, state["media_id"],
                                state, "tail_prog", source_id)
    return state["media_id"]


async def _do_send(client, channel, chunks, media, reply_to, forward_source,
                   state, annotation, source_id=None, data=None):
    if data and data.get("_inferred_album"):
        album_caption = data.get("_album_caption_text") or ""
        album_caption_entities = data.get("_album_caption_entities") or []
        if exportmedia.album_compatible(media):
            return await _send_album(
                client, channel, chunks, media, reply_to, state, source_id,
                force_document=all(kind == "document" for _path, kind in media),
                album_caption=album_caption,
                album_caption_entities=album_caption_entities,
                return_all=True,
            )

        # The source records still form one group even when Telegram cannot
        # represent the media combination as a native album. Preserve every
        # item in source order, attach the caption to the first captionable
        # item (or as a prefix for voice notes), and checkpoint each
        # destination ID so an interrupted sequence resumes without duplicates.
        first_caption, continuation = inferred_album_caption_parts(
            album_caption, album_caption_entities,
        )
        sequence_chunks = [first_caption] + continuation
        await _send_individual_media(
            client, channel, sequence_chunks, media, reply_to, state,
            source_id,
        )
        if continuation:
            last_media_id = state["multi_media_ids"][-1]
            await _send_text_chunks(
                client, channel, continuation, last_media_id, state,
                "album_caption_continuation_prog", source_id,
            )
        return list(state.get("multi_media_ids", []))

    forwarded_id = await _send_native_forward(
        client, channel, forward_source, reply_to, state, annotation, source_id
    )
    if forwarded_id is not None:
        return forwarded_id

    if len(media) > 1:
        return await _send_multiple_media(
            client, channel, chunks, media, reply_to, state, source_id
        )
    if not media:
        return await _send_text_only(client, channel, chunks, reply_to, state, source_id)
    return await _send_single_media(
        client, channel, chunks, media, reply_to, state, source_id
    )


async def send_one(client, channel, data, chunks, reply_to, state, annotation, source_id):
    attempt, floods = 0, 0
    while True:
        try:
            return await _do_send(
                client, channel, chunks, data["media"], reply_to,
                data["forward_source"], state, annotation,
                source_id, data,
            )
        except FloodWaitError as fw:
            floods += 1
            if floods > config.settings.max_flood_waits:
                reason = f"gave up after {config.settings.max_flood_waits} FloodWait errors"
                print(f"    failed id={data['msg_id']}: {reason}")
                progress.log_failure(data["msg_id"], reason)
                return None
            print(f"    FloodWait {fw.seconds}s — sleeping...")
            await asyncio.sleep(fw.seconds + 5)
        except Exception as exc:
            attempt += 1
            if attempt >= config.settings.max_retries:
                print(f"    failed id={data['msg_id']}: {exc}")
                progress.log_failure(data["msg_id"], str(exc))
                return None
            print(f"    retry {attempt}/{config.settings.max_retries} id={data['msg_id']}: {exc}")
            await asyncio.sleep(2 ** attempt)
