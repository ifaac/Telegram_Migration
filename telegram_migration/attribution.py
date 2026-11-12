"""Durable sender/date attribution for stickers and standalone emoji posts."""

import secrets

from telethon.tl import functions as fn
from telethon.tl import types as tl

from . import formatfallback, progress, uploads
from .textutil import clean_invalid_entities


def _sent_message_id_from_updates(result, random_id):
    """Get the server-assigned ID corresponding to one MTProto random_id."""
    if getattr(result, "id", None) is not None:
        return int(result.id)
    for update in getattr(result, "updates", ()) or ():
        if (getattr(update, "random_id", None) == random_id
                and getattr(update, "id", None) is not None):
            return int(update.id)
    for update in getattr(result, "updates", ()) or ():
        message = getattr(update, "message", None)
        if message is not None and getattr(message, "id", None) is not None:
            return int(message.id)
    return None


async def _send_attribution_reply(client, channel, target_message_id, text,
                                  entities, state, source_id, progress_key):
    """Attach attribution to a post, using a discussion reply when available.

    For broadcast channels with a linked discussion group, use MTProto's
    cross-chat InputReplyToMessage reference. If no discussion group exists,
    publish attribution as a separate channel post. The random_id and chosen
    route are checkpointed so retries reuse the same destination and ID.
    """
    progress_state = state.setdefault(progress_key, {})
    if progress_state.get("reply_message_id") is not None:
        return int(progress_state["reply_message_id"])

    if not progress_state.get("random_id"):
        progress_state["random_id"] = secrets.randbits(63) or 1
        progress.save_state(state["src"], state)

    random_id = int(progress_state["random_id"])
    entities = clean_invalid_entities(text, entities or [])

    if getattr(channel, "broadcast", False):
        channel_id = int(getattr(channel, "id"))
        access_hash = getattr(channel, "access_hash", None)
        if access_hash is None:
            raise RuntimeError(
                "Cannot reply to this channel post: Telegram did not provide "
                "the channel access hash."
            )
        input_channel = tl.InputChannel(
            channel_id=channel_id, access_hash=int(access_hash)
        )
        channel_peer = tl.InputPeerChannel(
            channel_id=channel_id, access_hash=int(access_hash)
        )
        # Lock the delivery route in the checkpoint before sending. If the
        # process stops and resumes, the same random_id must be retried against
        # the same peer (Telegram deduplicates random IDs per destination).
        standalone = progress_state.get("delivery_mode") == "standalone_channel_post"
        discussion_peer = None
        if not standalone:
            full = await client(fn.channels.GetFullChannelRequest(channel=input_channel))
            linked_chat_id = getattr(
                getattr(full, "full_chat", None), "linked_chat_id", None
            )
            if not linked_chat_id or getattr(channel, "noforwards", False):
                # A channel without a discussion group cannot host comments;
                # protected channel posts also cannot be cross-chat replied to.
                # In either case, preserve attribution as the next channel post.
                standalone = True
                progress_state["delivery_mode"] = "standalone_channel_post"
                progress.save_state(state["src"], state)
            else:
                discussion_chat = next(
                    (chat for chat in getattr(full, "chats", ()) or ()
                     if int(getattr(chat, "id", -1)) == int(linked_chat_id)),
                    None,
                )
                if discussion_chat is None or getattr(
                    discussion_chat, "access_hash", None
                ) is None:
                    raise RuntimeError(
                        "Telegram reported a linked discussion group but did not "
                        "return its access hash. The bot must have access to that group."
                    )
                discussion_peer = tl.InputPeerChannel(
                    channel_id=int(linked_chat_id),
                    access_hash=int(discussion_chat.access_hash),
                )
                progress_state["delivery_mode"] = "discussion_reply"
                progress.save_state(state["src"], state)

        if standalone:
            request = fn.messages.SendMessageRequest(
                peer=channel_peer,
                message=text,
                random_id=random_id,
                entities=entities or None,
                no_webpage=True,
            )
        else:
            reply_to = tl.InputReplyToMessage(
                reply_to_msg_id=int(target_message_id),
                reply_to_peer_id=channel_peer,
            )
            request = fn.messages.SendMessageRequest(
                peer=discussion_peer,
                message=text,
                random_id=random_id,
                reply_to=reply_to,
                entities=entities or None,
                no_webpage=True,
            )
        result = await client(request)
        reply_message_id = _sent_message_id_from_updates(result, random_id)
        if reply_message_id is None:
            raise RuntimeError(
                "Telegram accepted the attribution request but returned no "
                "message ID; the source message has not been marked complete. "
                "Restart to retry safely with the same random_id."
            )
    else:
        # In groups/supergroups the post and reply live in the same peer.
        sent = await formatfallback.with_formatting_fallback(
            lambda **kw: client.send_message(channel, text, **kw),
            {
                "reply_to": int(target_message_id),
                "formatting_entities": entities or None,
                "parse_mode": None,
            },
            text, entities, source_id,
        )
        reply_message_id = int(sent.id)

    progress_state["reply_message_id"] = int(reply_message_id)
    progress.save_state(state["src"], state)
    return int(reply_message_id)


async def _send_sticker_with_attribution_reply(
    client, channel, path, kind, reply_to, chunks, data, state, source_id,
):
    """Post a native sticker without caption, then reply with its attribution."""
    if "sticker_message_id" not in state:
        sticker = await uploads.send_media_item(
            client, channel, path, kind, reply_to, source_id,
            caption="", entities=None,
        )
        state["sticker_message_id"] = int(sticker.id)
        progress.save_state(state["src"], state)

    attribution_text = data.get("_attribution_reply_text")
    attribution_entities = data.get("_attribution_reply_entities") or []
    if not attribution_text:
        attribution_text, attribution_entities = chunks[0]

    await _send_attribution_reply(
        client, channel, state["sticker_message_id"],
        attribution_text, attribution_entities,
        state, source_id, "sticker_attribution_reply",
    )
    return int(state["sticker_message_id"])


async def _send_emoji_with_attribution_reply(
    client, channel, data, reply_to, state, source_id,
):
    """Post an emoji-only text message, then reply with sender/date."""
    if "emoji_message_id" not in state:
        body = data.get("body_text") or ""
        entities = list(data.get("body_entities") or [])
        sent = await formatfallback.with_formatting_fallback(
            lambda **kw: client.send_message(channel, body, **kw),
            {
                "reply_to": reply_to,
                "formatting_entities": entities or None,
                "parse_mode": None,
            },
            body, entities, source_id,
        )
        state["emoji_message_id"] = int(sent.id)
        progress.save_state(state["src"], state)

    header_text, header_entities = data.get("_emoji_reply_header", ("", []))
    header_text = (header_text or "").rstrip()
    await _send_attribution_reply(
        client, channel, state["emoji_message_id"],
        header_text, header_entities or [],
        state, source_id, "emoji_attribution_reply",
    )
    return int(state["emoji_message_id"])


