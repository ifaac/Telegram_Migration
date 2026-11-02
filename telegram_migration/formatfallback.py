"""Retry a send without formatting when Telegram rejects the message entities."""

from telethon.errors import FloodWaitError

from . import config, progress
from .textutil import u16len


# Source id of the message being sent (for diagnostics only).
_current_src = [None]


def _is_entity_error(exc):
    """True when Telegram/Telethon rejected the formatting entities of a message.

    Matches on the exception class name (all entity exceptions Telethon raises
    contain "Entit") and on Telegram's own wording. Deliberately does NOT match
    the bare word "entities" in the message, because unrelated exceptions might
    contain it and would then be routed into the formatting-fallback path.
    """
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return ("entit" in name or "invalid bounds" in msg or "entity_bounds" in msg)


def note_formatting_dropped(src, text, entities, exc):
    """Record why formatting was dropped, so the real cause can be found."""
    try:
        progress._append_jsonl(config.settings.format_debug_file, {
            "src": src,
            "error": f"{type(exc).__name__}: {exc}",
            "text_utf16_len": u16len(text or ""),
            "text_repr": (text or "")[:400],
            "entities": [
                {"type": type(e).__name__, "offset": getattr(e, "offset", None),
                 "length": getattr(e, "length", None)}
                for e in (entities or [])[:60]
            ],
        })
    except Exception:
        pass
    print(f"    note: Telegram rejected the formatting of source id={src} "
          f"({type(exc).__name__}); sending it as plain text (details: {config.settings.format_debug_file})")


async def _with_formatting_fallback(send, kwargs, text, entities, src=None):
    """Run send(**kwargs); if the formatting is rejected, retry once without it.

    Formatting is cosmetic, the content is not: a message must not fail (and be
    retried five times) because of an entity problem. FloodWait is never
    swallowed here.
    """
    try:
        return await send(**kwargs)
    except FloodWaitError:
        raise
    except Exception as exc:
        if entities and (_is_entity_error(exc) or isinstance(exc, (TypeError, ValueError))):
            note_formatting_dropped(src, text, entities, exc)
            retry = dict(kwargs)
            retry["formatting_entities"] = None
            return await send(**retry)
        raise
