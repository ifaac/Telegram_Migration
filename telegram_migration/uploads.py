"""Uploading media: send_file compatibility, sticker options and per-item upload."""

import inspect
import os
import re

from telethon import TelegramClient
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
        metadata = mediatools._probe_video_metadata(path) or {}
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
    per-call TypeError fallback in _send_file() does the work.
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


async def _send_file(client, channel, files, **kwargs):
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


async def _send_media_item(client, channel, path, kind, reply_to,
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
        # Telegram GIFs are documents marked with DocumentAttributeAnimated.
        # For exported silent MP4 animations, nosound_video=False asks Telegram
        # to display it as a GIF. For actual .gif files, the Animated attribute
        # asks Telegram to convert the uploaded GIF to its native MP4 form.
        kwargs["force_document"] = True
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
        if mediatools._video_has_audio(path) is False:
            kwargs["nosound_video"] = True
    elif kind == "voice":
        kwargs["voice_note"] = True
    elif kind == "round_video":
        kwargs["video_note"] = True
    elif kind == "document":
        # Telethon auto-detects .jpg/.png files as photos by extension unless
        # force_document is set. For files/ attachments, preserve the original
        # document/file presentation rather than uploading a compressed photo.
        kwargs["force_document"] = True

    sent = await formatfallback._with_formatting_fallback(
        lambda **kw: _send_file(client, channel, path, **kw),
        kwargs, caption, entities, formatfallback._current_src[0],
    )
    return sent[0] if isinstance(sent, (list, tuple)) else sent
