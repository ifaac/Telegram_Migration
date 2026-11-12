"""Verify and repair media captions on the media post itself."""

from telethon.errors import FloodWaitError, MessageNotModifiedError

from . import formatfallback


def _message_text(message):
    if message is None:
        return None
    value = getattr(message, "message", None)
    return value if value is not None else getattr(message, "text", None)


async def get_message_by_id(client, channel, message_id):
    getter = getattr(client, "get_messages", None)
    if not callable(getter):
        return None
    message = await getter(channel, ids=int(message_id))
    if isinstance(message, (list, tuple)):
        return message[0] if message else None
    return message


async def ensure_media_caption(client, channel, message, caption, entities, source_id):
    """Confirm Telegram retained a media caption; repair it on the same message."""
    current_text = _message_text(message)
    # A test double or an old checkpoint may not expose message text. Do not
    # treat that as proof that Telegram dropped the caption.
    if not caption or current_text is None or current_text == caption:
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

