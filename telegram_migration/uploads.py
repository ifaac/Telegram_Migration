"""Uploading media: send_file compatibility, sticker options and per-item upload."""

import inspect
import os
import re

from telethon import TelegramClient
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.tl import types as tl

from . import formatfallback, mediatools
from .textutil import clean_invalid_entities
from .constants import STICKER_KINDS


def _sticker_upload_options(path, kind):
    """Build Telethon options that mark a file as a Telegram sticker."""
    ext = os.path.splitext(path)[1].casefold()
    sticker_attr_cls = getattr(tl, "DocumentAttributeSticker", None)
    empty_set_cls = getattr(tl, "InputStickerSetEmpty", None)
    if sticker_attr_cls is None or empty_set_cls is None:
        raise RuntimeError(
            "This Telethon version lacks sticker upload attributes; upgrade Telethon."
        )

    attributes = [sticker_attr_cls(alt="🙂", stickerset=empty_set_cls())]
    options = {
        "force_document": True,
        "attributes": attributes,
    }

    if kind == "sticker_animated" or ext == ".tgs":
        options["mime_type"] = "application/x-tgsticker"
    elif kind == "sticker_video" or ext == ".webm":
        options["mime_type"] = "video/webm"
        metadata = mediatools.probe_video_metadata(path) or {}
        width = int(metadata.get("width") or 512)
        height = int(metadata.get("height") or 512)
        duration = float(metadata.get("duration") or 1.0)
        video_attr_cls = getattr(tl, "DocumentAttributeVideo", None)
        if video_attr_cls is None:
            raise RuntimeError(
                "This Telethon version lacks video attributes required for video stickers."
            )
        try:
            video_attr = video_attr_cls(
                duration=duration, w=width, h=height,
                supports_streaming=False, nosound=True,
            )
        except TypeError:
            # Compatibility with older Telethon schema layers that lack `nosound`.
            video_attr = video_attr_cls(
                duration=duration, w=width, h=height,
                supports_streaming=False,
            )
        attributes.append(video_attr)
        # Keep the upload running if a file is malformed or ffprobe cannot read it.
        if not metadata:
            print(f"    warning: could not read video metadata for sticker {os.path.basename(path)}; using 512x512 / 1s fallback")
        options["nosound_video"] = True
    elif ext == ".webp":
        options["mime_type"] = "image/webp"
    elif ext == ".png":
        options["mime_type"] = "image/png"
    else:
        # Unknown files from the stickers directory are still preserved as a
        # document instead of blocking the whole migration, but cannot be
        # guaranteed to display as a Telegram sticker.
        print(f"    warning: unknown sticker format {ext or '(no extension)'}; sending as a document")
        options.pop("attributes", None)
        options["mime_type"] = None

    return options


# send_file options that some Telethon builds do not accept. They are dropped
# (once, remembered) when the installed Telethon rejects them with a TypeError.
_OPTIONAL_SEND_KWARGS = ("mime_type", "nosound_video")


_UNSUPPORTED_SEND_KWARGS = set()


def configure_send_file_compat():
    """Look at the installed send_file() signature and note options it lacks. Never aborts.

    If the signature takes **kwargs (or cannot be read) nothing is assumed and the
    per-call TypeError fallback in send_file() does the work.
    """
    try:
        params = inspect.signature(TelegramClient.send_file).parameters
    except (TypeError, ValueError, AttributeError):
        return
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return
    for name in _OPTIONAL_SEND_KWARGS:
        if name not in params:
            _UNSUPPORTED_SEND_KWARGS.add(name)
    if _UNSUPPORTED_SEND_KWARGS:
        print("Note: this Telethon's send_file() lacks: " + ", ".join(sorted(_UNSUPPORTED_SEND_KWARGS))
              + ". Those options will be skipped (upgrade Telethon for best GIF/sticker fidelity).")


async def send_file(client, channel, files, **kwargs):
    """client.send_file() that tolerates optional keywords an older Telethon rejects."""
    for key in list(kwargs):
        if key in _UNSUPPORTED_SEND_KWARGS:
            kwargs.pop(key)
    while True:
        try:
            return await client.send_file(channel, files, **kwargs)
        except TypeError as exc:
            m = re.search(r"unexpected keyword argument '(\w+)'", str(exc))
            bad = m.group(1) if m else None
            if bad in _OPTIONAL_SEND_KWARGS and bad in kwargs:
                _UNSUPPORTED_SEND_KWARGS.add(bad)
                kwargs.pop(bad)
                print(f"    note: this Telethon does not accept send_file({bad}=...); skipping that option from now on")
                continue
            raise


def _caption_message_text(message):
    if message is None:
        return None
    value = getattr(message, "message", None)
    if value is None:
        value = getattr(message, "text", None)
    return value


def _single_message(value):
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


async def ensure_media_caption(client, channel, message_id, caption, entities=None,
                               source_id=None, message=None):
    """Verify the caption by fetching the stored destination message.

    The object returned by send_file() is not accepted as proof that Telegram
    persisted the caption: some upload paths can return a local response object
    that echoes the requested text. Always re-fetch by destination ID. If the
    stored message lacks the caption, edit that same media message and fetch it
    again. Never creates a separate text message.

    Callers checkpoint the destination message ID before invoking this helper,
    so a failed verification can be retried without uploading duplicate media.
    """
    if not caption:
        return message

    async def fetch_message():
        getter = getattr(client, "get_messages", None)
        if not callable(getter):
            raise RuntimeError(
                f"Cannot independently verify caption for source id={source_id}; "
                "Telegram client does not expose get_messages"
            )
        result = await getter(channel, ids=message_id)
        stored = _single_message(result)
        if stored is None:
            raise RuntimeError(
                f"Cannot retrieve destination media message id={message_id} "
                f"for source id={source_id}; refusing to assume its caption was saved"
            )
        return stored

    # Deliberately ignore the caller-supplied send_file() object for verification.
    stored = await fetch_message()
    if _caption_message_text(stored) == caption:
        return stored

    editor = getattr(client, "edit_message", None)
    if not callable(editor):
        raise RuntimeError(
            f"Telegram did not retain the caption for source id={source_id}; "
            f"destination media message id={message_id} cannot be edited"
        )

    try:
        await editor(
            channel, message_id, caption,
            formatting_entities=entities or None, parse_mode=None,
        )
    except FloodWaitError:
        raise
    except MessageNotModifiedError:
        # Still re-fetch below; never treat the local response as verification.
        pass
    except Exception as exc:
        raise RuntimeError(
            f"Telegram did not retain the caption for source id={source_id}; "
            f"could not attach it to existing media message id={message_id}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    verified = await fetch_message()
    if _caption_message_text(verified) == caption:
        return verified

    raise RuntimeError(
        f"Caption verification failed for source id={source_id}, "
        f"destination media message id={message_id}; sender/date and message "
        "text were not retained on the media message"
    )


async def send_media_item(client, channel, path, kind, reply_to, source_id=None,
                           caption="", entities=None):
    """Send one media item with format-specific GIF/sticker handling."""
    caption = caption or ""
    # Enforce Telegram's entity-bounds invariants on captions too.
    entities = clean_invalid_entities(caption, entities)
    kwargs = {
        "reply_to": reply_to,
        "caption": caption,
        "formatting_entities": entities or None,
        "parse_mode": None,          # our caption is final; do not re-parse it as markdown
    }

    if kind in STICKER_KINDS:
        sticker_options = _sticker_upload_options(path, kind)
        kwargs.update(sticker_options)
    elif kind == "gif":
        # Telegram represents GIFs internally as animated documents, but they
        # must not be force-uploaded as generic files: that sets Telegram's
        # force_file flag and makes the animation appear as a download.
        # The Animated attribute identifies actual .gif uploads; for exported
        # silent MP4 animations, nosound_video=False asks Telegram to display
        # the video as an inline animation.
        kwargs["attributes"] = [tl.DocumentAttributeAnimated()]
        ext = os.path.splitext(path)[1].casefold()
        if ext == ".gif":
            kwargs["mime_type"] = "image/gif"
        else:
            kwargs["mime_type"] = "video/mp4"
            kwargs["nosound_video"] = False
    elif kind == "video":
        # Only a video that really has no audio track needs the "this is a video,
        # not a GIF" flag. Videos with sound (or when ffprobe cannot tell) are
        # sent without it, so no file is mislabelled as silent.
        if mediatools.video_has_audio(path) is False:
            kwargs["nosound_video"] = True
    elif kind == "voice":
        kwargs["voice_note"] = True
    elif kind == "round_video":
        if caption:
            # Telegram video notes have no caption field in the bot send API.
            # Keep the forwarded attribution and payload in the same Telegram
            # message by uploading captioned round-video media as a regular
            # video. Uncaptioned round videos retain their circular presentation.
            print(
                f"    note: captioned round video {os.path.basename(path)} "
                "is being sent as a regular video so its caption stays attached"
            )
        else:
            kwargs["video_note"] = True
    elif kind == "document":
        # Telethon auto-detects .jpg/.png files as photos by extension unless
        # force_document is set. For files/ attachments, preserve the original
        # document/file presentation rather than uploading a compressed photo.
        kwargs["force_document"] = True

    sent = await formatfallback.with_formatting_fallback(
        lambda **kw: send_file(client, channel, path, **kw),
        kwargs, caption, entities, source_id,
    )
    return sent[0] if isinstance(sent, (list, tuple)) else sent
