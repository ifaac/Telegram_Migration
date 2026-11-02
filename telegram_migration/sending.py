"""Sending one migrated message: text chunks, albums, voice/stickers, native forwards."""

import asyncio
import os

from telethon.errors import FloodWaitError
from telethon.tl import types as tl

from . import config, exportmedia, formatfallback, progress, uploads
from .textutil import clean_invalid_entities
from .constants import NO_CAPTION_KINDS, STICKER_KINDS


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


async def _try_native_forward(client, channel, peer_hint, source_msg_id):
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
            print(f"    native forward not possible ({type(exc).__name__}: {exc}); "
                  f"using the textual header (further notices suppressed)")
        return None


async def _send_text_chunks(client, channel, chunks, reply_to, state, prog_key, persist=True):
    """Send each text chunk in order, remembering progress.

    Takes the whole `state` so each successful chunk can be checkpointed to
    disk (when `persist` is true). A crash between chunks then resumes at the
    next unsent chunk rather than replaying from the top. Single-chunk sends
    pass persist=False: there is nothing to resume in the middle of.
    """
    prog = state.setdefault(prog_key, {})
    # If a persisted run used different chunking, `done` may not correspond to
    # any real chunk boundary in this run. Reset it whenever it looks stale.
    done = prog.get("done", 0)
    if done > len(chunks):
        print(f"    note: resetting stale {prog_key}.done={done} for src={state.get('src')} (current chunk count {len(chunks)})")
        prog.clear()
    for i, (text, ents) in enumerate(chunks):
        if i < prog.get("done", 0):
            continue
        # Enforce Telegram's entity-bounds invariants before every send.
        ents = clean_invalid_entities(text, ents)
        # parse_mode=None: our text is final; Telethon must not re-parse it as markdown.
        m = await formatfallback._with_formatting_fallback(
            lambda **kw: client.send_message(channel, text, **kw),
            {"formatting_entities": ents or None,
             "reply_to": prog.get("last", reply_to),
             "parse_mode": None},
            text, ents, formatfallback._current_src[0],
        )
        prog.setdefault("first", m.id)
        prog["last"] = m.id
        prog["done"] = i + 1
        src = state.get("src")
        if persist and src is not None:
            progress.save_state(src, state)
    return prog["first"]


async def _do_send(client, channel, chunks, media, reply_to, forward_source,
                   state, annotation):
    if (forward_source is not None and config.settings.native_forward
            and reply_to is None and not state.get("force_reconstruction")):
        new_id = state.get("native_id")
        if new_id is None and not state.get("fwd_tried"):
            peer_hint, src_id = forward_source
            if peer_hint != "__text__" and isinstance(src_id, int):
                new_id = await _try_native_forward(client, channel, peer_hint, src_id)
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
                                        state, "ann_prog")
            return new_id

    first_text, first_ents = chunks[0]
    rest = chunks[1:]

    if len(media) > 1:
        # GIFs and stickers cannot be reliably sent as ordinary media albums.
        # Send such groups item-by-item. If a sticker is present, preserve the
        # timestamp/sender/body as a text message first because stickers do not
        # support captions.
        has_sticker = any(kind in STICKER_KINDS for _path, kind in media)
        # File attachments with image extensions must bypass album auto-detection:
        # force_document is a per-upload option, not a per-item option for a mixed
        # album, so send these individually to preserve their document form.
        image_file_extensions = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
        has_document_image = any(
            kind == "document" and os.path.splitext(path)[1].casefold() in image_file_extensions
            for path, kind in media
        )
        individual_media = (
            has_sticker or has_document_image
            or any(kind in ("gif", "voice", "round_video") for _path, kind in media)
            or not exportmedia._album_compatible(media)       # e.g. documents mixed with photos/videos
        )

        if individual_media:
            needs_prefix_text = has_sticker or media[0][1] in NO_CAPTION_KINDS
            if needs_prefix_text:
                if "prefix_text_id" not in state:
                    # Reuse a previously persisted text ID after restarting the
                    # migration rather than sending the archival text twice.
                    saved_text_id = state.get("text_id")
                    if saved_text_id is not None:
                        state["prefix_text_id"] = saved_text_id
                        progress.save_state(state["src"], state)
                    elif any(t.strip() for t, _ in chunks):
                        text_id = await _send_text_chunks(
                            client, channel, chunks, reply_to,
                            state, "prefix_prog",
                        )
                        state["prefix_text_id"] = text_id
                        state["text_id"] = text_id
                        progress.save_state(state["src"], state)
                media_reply_to = state.get("prefix_text_id") or reply_to
                caption_first = False
            else:
                media_reply_to = reply_to
                caption_first = True

            state.setdefault("multi_media_ids", [])
            while len(state["multi_media_ids"]) < len(media):
                index = len(state["multi_media_ids"])
                path_i, kind_i = media[index]
                caption_i = first_text if (index == 0 and caption_first) else ""
                entities_i = first_ents if (index == 0 and caption_first) else None
                sent_msg = await uploads._send_media_item(
                    client, channel, path_i, kind_i, media_reply_to,
                    caption=caption_i, entities=entities_i,
                )
                state["multi_media_ids"].append(sent_msg.id)
                progress.save_state(state["src"], state)
            state["album_id"] = state["multi_media_ids"][0]
            progress.save_state(state["src"], state)
        elif "album_id" not in state:
            files = [m[0] for m in media]
            captions = [first_text] + [""] * (len(files) - 1)
            # Telethon's album path may ignore formatting_entities (it can parse
            # the captions itself), so parse_mode=None keeps our text literal
            # either way. Formatting is tried once; if Telegram rejects it the
            # album is resent without it. FloodWait still propagates.
            safe_first_ents = clean_invalid_entities(first_text, first_ents)
            album_kwargs = {"caption": captions, "reply_to": reply_to, "parse_mode": None}
            if safe_first_ents:
                album_kwargs["formatting_entities"] = safe_first_ents
            msgs = await formatfallback._with_formatting_fallback(
                lambda **kw: uploads._send_file(client, channel, files, **kw),
                album_kwargs, first_text, safe_first_ents, formatfallback._current_src[0],
            )

            # Persist the album's first message ID in state immediately. This
            # avoids re-uploading it if a later text chunk fails in this run.
            if not msgs:
                raise RuntimeError("Album upload returned no messages")
            first_msg = msgs[0] if isinstance(msgs, (list, tuple)) else msgs
            state["album_id"] = first_msg.id
            progress.save_state(state["src"], state)

        # When a prefix text was needed, all text chunks were sent before the
        # individual media items. Otherwise, any overflow follows the first item.
        needs_prefix_text = individual_media and (
            has_sticker or media[0][1] in NO_CAPTION_KINDS
        )
        if rest and not needs_prefix_text:
            await _send_text_chunks(client, channel, rest, state["album_id"],
                                    state, "tail_prog")
        if needs_prefix_text:
            return state.get("prefix_text_id") or state["album_id"]
        return state["album_id"]

    if media and media[0][1] in NO_CAPTION_KINDS:
        path, kind = media[0]
        text_id = state.get("text_id")
        if text_id is None and any(t.strip() for t, _ in chunks):
            text_id = await _send_text_chunks(client, channel, chunks, reply_to,
                                              state, "text_prog")
            state["text_id"] = text_id
            progress.save_state(state["src"], state)
        thread = text_id or reply_to
        if "media_id" not in state:
            media_msg = await uploads._send_media_item(client, channel, path, kind, thread)
            state["media_id"] = media_msg.id
            progress.save_state(state["src"], state)
        return text_id or state["media_id"]

    if not media:
        # A single-chunk text has no half-finished state worth writing to disk.
        return await _send_text_chunks(client, channel, chunks, reply_to,
                                       state, "text_prog", persist=len(chunks) > 1)

    path, kind = media[0]
    if "media_id" not in state:
        media_msg = await uploads._send_media_item(
            client, channel, path, kind, reply_to,
            caption=first_text, entities=first_ents,
        )
        state["media_id"] = media_msg.id
        # Always persist, so a crash between the upload returning and the id-map
        # write cannot re-upload the same media on the next run.
        progress.save_state(state["src"], state)
    if rest:
        await _send_text_chunks(client, channel, rest, state["media_id"],
                                state, "tail_prog")
    return state["media_id"]


async def send_one(client, channel, data, chunks, reply_to, state, annotation):
    attempt, floods = 0, 0
    while True:
        try:
            return await _do_send(
                client, channel, chunks, data["media"], reply_to,
                data["forward_source"], state, annotation,
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
