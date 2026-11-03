"""Find and classify the media files of an exported message."""

import os

from . import config
from .htmlutil import css_classes, find_first


_GIF_LABELS = ("gif", "animation", "animated gif")


def _label_says_gif(label):
    """True only when a whole line of the export's media block is the GIF/Animation label.

    A file name or title that merely contains the word "GIF" (for example
    "my GIF collection.mp4") must not make an ordinary video a GIF.
    """
    prefixes = tuple(x + "," for x in _GIF_LABELS)
    for line in str(label or "").splitlines():
        t = line.strip().casefold()
        if t in _GIF_LABELS or t.startswith(prefixes):
            return True
    return False


def _is_gif(path, anchor_classes=(), export_label=""):
    """Identify GIFs only from explicit export metadata or a .gif filename.

    Telegram Desktop commonly stores animations as silent MP4 files. Their
    export markup may flag them as animated or label the media as GIF /
    Animation. A short, silent MP4 by itself is not enough evidence.
    """
    classes = {str(c).casefold() for c in anchor_classes}
    if any(c == "gif" or "animated" in c for c in classes):
        return True
    if os.path.splitext(str(path))[1].casefold() == ".gif":
        return True
    return _label_says_gif(export_label)


def _classify(rel, wrap_class, full, anchor_classes=(), export_label=""):
    """Classify export media using both wrapper/anchor markup and file extension."""
    rel = rel.replace("\\", "/")
    lower_rel = rel.casefold()
    classes = {str(c).casefold() for c in anchor_classes}
    is_sticker = (
        wrap_class == "sticker_wrap"
        or "sticker_wrap" in classes
        or "stickers/" in lower_rel
    )
    ext = os.path.splitext(lower_rel)[1]

    if is_sticker:
        if ext in (".webp", ".png"):
            return "sticker_static"
        if ext == ".tgs":
            return "sticker_animated"
        if ext == ".webm":
            return "sticker_video"
        return "sticker_unknown"

    if wrap_class == "roundvideo_file_wrap" or "round_video_messages" in lower_rel:
        return "round_video"
    if "voice_messages" in lower_rel:
        return "voice"
    if ext == ".gif":
        return "gif"
    if "video_files" in lower_rel or ext in (".mp4", ".mov", ".m4v", ".avi"):
        return "gif" if _is_gif(full, anchor_classes, export_label) else "video"
    # Telegram Desktop distinguishes actual photos by their photos/ export path
    # and/or photo-specific markup. Do not infer "photo" solely from .jpg/.png:
    # those extensions can also be ordinary files whose document form matters.
    photo_markup = any(
        c in {"photo", "photo_wrap", "photo_file_wrap"} or c.startswith("photo_")
        for c in classes
    )
    if "photos/" in lower_rel or photo_markup:
        return "photo"
    return "document"


def find_media(msg):
    results, seen = [], set()
    # The sticker_wrap class is sometimes on the link inside media_wrap rather
    # than on the wrapper itself, so _classify also checks anchor/image classes.
    for wrap_class in ("media_wrap", "sticker_wrap", "roundvideo_file_wrap"):
        wrap = find_first(msg, wrap_class)
        if wrap is None:
            continue
        # One line per text node, so GIF/Animation labels can be matched as whole lines.
        export_label = "\n".join(t.strip() for t in wrap.itertext() if t and t.strip())
        found_in_wrapper = False
        for a in wrap.iter("a"):
            href = a.get("href")
            if not href:
                continue
            path = os.path.join(config.settings.base_dir, href)
            if os.path.exists(path) and path not in seen:
                seen.add(path)
                child_image_classes = [
                    cls for img in a.iter("img") for cls in css_classes(img)
                ]
                results.append((path, _classify(
                    href, wrap_class, path,
                    css_classes(wrap) + css_classes(a) + child_image_classes,
                    export_label,
                )))
                found_in_wrapper = True
        if not found_in_wrapper:
            for img in wrap.iter("img"):
                src = img.get("src")
                if not src:
                    continue
                path = os.path.join(config.settings.base_dir, src)
                if os.path.exists(path) and path not in seen:
                    seen.add(path)
                    results.append((path, _classify(
                        src, wrap_class, path, css_classes(wrap) + css_classes(img), export_label
                    )))
    return results


def media_note(msg_div):
    wrap = find_first(msg_div, "media_wrap")
    if wrap is None:
        return ""
    lines = [t.strip() for t in wrap.itertext() if t and t.strip()]
    if not lines:
        return ""
    note = " | ".join(lines)
    return "[" + (note[:500] + "..." if len(note) > 500 else note) + "]"


def album_compatible(media):
    """Telegram albums hold photos/videos together, or documents together. Nothing else."""
    kinds = {kind for _path, kind in media}
    if kinds <= {"photo", "video"}:
        return True
    if kinds == {"document"}:
        return True
    return False
