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
    """Send exported sticker assets as ordinary caption-capable documents.

    Deliberately omit DocumentAttributeSticker and video/sticker attributes:
    Telegram must treat these as files, not native stickers, so the sender/date
    caption is part of the same visible document message.
    """
    ext = os.path.splitext(path)[1].casefold()
    options = {"force_document": True}

    if kind == "sticker_animated" or ext == ".tgs":
        options["mime_type"] = "application/x-tgsticker"
    elif kind == "sticker_video" or ext == ".webm":
        options["mime_type"] = "video/webm"
    elif ext == ".webp":
        options["mime_type"] = "image/webp"
    elif ext == ".png":
        options["mime_type"] = "image/png"
    else:
        # Preserve unfamiliar sticker-directory assets as ordinary files too.
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
    message = sent[0] if isinstance(sent, (list, tuple)) else sent
    if kind in STICKER_KINDS and caption:
        # Verify Telegram returned the caption on the same document message.
        actual_caption = getattr(message, "message", None)
        if actual_caption != caption:
            raise RuntimeError(
                f"Telegram did not retain the caption on sticker file "
                f"{os.path.basename(path)} (source id={source_id}); "
                f"expected {caption!r}, received {actual_caption!r}"
            )
    return message
