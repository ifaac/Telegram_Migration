"""Read one message out of a Telegram Desktop HTML export."""

import os
import re
from datetime import datetime
from urllib.parse import parse_qs
from urllib.parse import urlparse

from . import config, exportmedia
from .htmlutil import css_classes, find_first, inner_text, text_without
from .richtext import parse_rich_text, parse_rich_text_with_stickers
from .textutil import sanitize_text
from .constants import HTML_FILE_PATTERN


_DATE_SUFFIX = re.compile(r"\s*UTC[+-]\d{2}:\d{2}\s*$|\s*UTC\s*$")


def discover_export_files(base_dir):
    """Return Telegram Desktop HTML export pages in message order."""
    if not os.path.isdir(base_dir):
        return []
    pattern = re.compile(HTML_FILE_PATTERN)
    found = []
    for name in os.listdir(base_dir):
        match = pattern.match(name)
        if match:
            found.append((int(match.group(1) or 1), os.path.join(base_dir, name)))
    return [path for _, path in sorted(found)]


def parse_export_date(s):
    if not s:
        return None
    s = _DATE_SUFFIX.sub("", s).strip()
    for fmt in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d %B %Y",
        "%d %b %Y",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _forwarded_body(msg_div):
    """Return Telegram Desktop's forwarded-message body, not its avatar.

    Desktop exports commonly place a ``forwarded userpic_wrap`` before the
    actual ``forwarded body``. Selecting the first element with the
    ``forwarded`` class therefore finds only the avatar and loses attribution.
    Prefer the explicit body class, with a guarded fallback for older export
    variants that omit it.
    """
    forwarded_elements = []
    for el in msg_div.iter():
        if not isinstance(el.tag, str):
            continue
        classes = css_classes(el)
        if "forwarded" not in classes:
            continue
        if "body" in classes:
            return el
        forwarded_elements.append(el)

    for el in forwarded_elements:
        classes = css_classes(el)
        if "userpic_wrap" in classes:
            continue
        if find_first(el, "from_name") is not None or find_first(el, "text") is not None:
            return el
    return None


def _forward_name(fwd_div):
    if fwd_div is None:
        return ""
    from_name = find_first(fwd_div, "from_name")
    a = next(from_name.iter("a"), None) if from_name is not None else None
    if a is not None and inner_text(a):
        return inner_text(a)
    return text_without(from_name, "date")


def _forward_timestamp(fwd_div):
    """Return the original forwarded-message timestamp from Desktop HTML.

    Telegram Desktop places this in a ``date details`` span nested inside
    the forwarded author's ``from_name`` element. Some export variants have
    a useful ``title`` attribute while others only expose visible text.
    """
    if fwd_div is None:
        return None, False, False
    from_name = find_first(fwd_div, "from_name")
    if from_name is None:
        return None, False, False
    for el in from_name.iter():
        if not isinstance(el.tag, str) or "date" not in css_classes(el):
            continue
        raw = (el.get("title") or "").strip()
        visible = inner_text(el).strip()
        parsed = parse_export_date(raw) if raw else None
        if parsed is None and visible:
            raw = visible
            parsed = parse_export_date(visible)
        if parsed is not None:
            has_time = bool(re.search(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", raw))
            has_seconds = bool(re.search(r"\b\d{1,2}:\d{2}:\d{2}\b", raw))
            return parsed, has_time, has_seconds
    return None, False, False


def extract_forward_source(msg_div):
    fwd_div = _forwarded_body(msg_div)
    if fwd_div is None:
        return None

    name = _forward_name(fwd_div)
    author_a = next(fwd_div.iter("a"), None)
    href = (author_a.get("href") if author_a is not None else "") or ""

    if href.startswith("https://t.me/") or href.startswith("http://t.me/"):
        m = re.match(r"https?://t\.me/(?:c/)?([^/?#]+)/(\d+)", href)
        if m and not href.startswith(("https://t.me/c/", "http://t.me/c/")):
            return (m.group(1), int(m.group(2)))
        return ("__text__", name)

    if href.startswith("tg://openmessage"):
        qs = parse_qs(urlparse(href).query)
        uid = qs.get("user_id", [None])[0]
        mid = qs.get("message_id", [None])[0]
        if uid and mid and mid.isdigit():
            return (str(uid), int(mid))
        return ("__text__", name)

    return ("__text__", name)


def _extract_html_reactions(msg_div):
    """Best-effort extraction for reaction markup, if a particular export has it."""
    found = []
    seen = set()
    for el in msg_div.iter():
        if not isinstance(el.tag, str):
            continue
        classes = {c.casefold() for c in css_classes(el)}
        if not any("reaction" in c for c in classes):
            continue
        # Skip container nodes; their children carry the individual emoji/count.
        if any(c in {"reactions", "message_reactions", "reactions_wrap", "reaction_wrap", "reactions-container"}
               for c in classes):
            continue

        raw_attrs = {
            k: v for k, v in el.attrib.items()
            if k.casefold().startswith("data-") or k.casefold() in ("title", "aria-label", "alt")
        }
        label = (el.get("data-emoji") or el.get("data-reaction") or el.get("emoji") or
                 el.get("aria-label") or el.get("title") or "").strip()
        if not label:
            for img in el.iter("img"):
                label = (img.get("alt") or img.get("title") or img.get("data-emoji") or "").strip()
                if label:
                    break
        text = inner_text(el)
        count = None
        for attr in ("data-count", "data-reaction-count", "count"):
            value = el.get(attr)
            if value and str(value).isdigit():
                count = int(value)
                break
        if count is None:
            match = re.search(r"(?:^|\s)(\d+)\s*$", text)
            if match:
                count = int(match.group(1))
        if not label:
            label = re.sub(r"\s*\d+\s*$", "", text).strip()
        if not label or label.isdigit():
            continue
        key = (label, count)
        if key in seen:
            continue
        seen.add(key)
        found.append({"label": label, "count": count, "actors": [], "raw": raw_attrs, "source": "html"})
    return found


def _reply_preview(reply_div):
    if reply_div is None:
        return ""
    text = inner_text(reply_div)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def _find_outer_reply_to(msg_div, forwarded_body=None):
    """Find a reply attached to the exported chat message, not its forwarded payload.

    Telegram Desktop can include a reply preview inside the forwarded body.
    That preview belongs to the message being forwarded; it is not a reply in
    the current chat. Exclude the complete forwarded subtree for every payload
    type so photos, videos, stickers, documents, and audio are treated alike.
    """
    forwarded_nodes = set(forwarded_body.iter()) if forwarded_body is not None else set()
    for el in msg_div.iter():
        if el is msg_div or el in forwarded_nodes:
            continue
        if "reply_to" in css_classes(el):
            return el
    return None


def _find_forward_reply_to(forwarded_body):
    """Find the reply preview embedded in the forwarded payload itself.

    This is original-source context. Preserve it for display, but never use its
    referenced message ID to create a reply link in the destination channel.
    """
    if forwarded_body is None:
        return None
    for el in forwarded_body.iter():
        if "reply_to" in css_classes(el):
            return el
    return None


def _extract_reply_source_id(reply_div):
    """Extract the source target from an already-selected outer reply element."""
    if reply_div is None:
        return None
    a = next(reply_div.iter("a"), None)
    if a is None:
        return None
    match = re.search(r"#go_to_message(\d+)", a.get("href") or "")
    return match.group(1) if match else None


def extract_date_separator(msg_div):
    """Return the calendar date from Telegram Desktop's negative-ID date divider.

    Telegram Desktop writes date dividers as ``message service`` elements with
    negative IDs (for example ``message-1``). Actual service events retain their
    positive message IDs, so they can be distinguished and migrated separately.
    """
    classes = css_classes(msg_div)
    raw_id = msg_div.get("id", "")
    if "message" not in classes or "service" not in classes:
        return None
    if not re.fullmatch(r"message-\d+", raw_id):
        return None
    body = find_first(msg_div, "body")
    label = inner_text(body if body is not None else msg_div)
    return parse_export_date(label)


def extract_service_event_data(msg_div, current_date=None):
    """Extract a real Telegram service event from a Desktop HTML export.

    Negative ``message-N`` records are date separators, not Telegram events;
    callers should process them with :func:`extract_date_separator` instead.
    """
    classes = css_classes(msg_div)
    if "message" not in classes or "service" not in classes:
        return None

    raw_id = msg_div.get("id", "")
    match = re.fullmatch(r"message(\d+)", raw_id)
    if not match:
        return None

    body = find_first(msg_div, "body")
    body_text, body_entities = parse_rich_text(body if body is not None else msg_div)
    body_text = body_text.strip()
    if not body_text:
        return None

    return {
        "msg_id": match.group(1),
        "joined": False,
        "timestamp": current_date,
        "timestamp_date_only": current_date is not None,
        "service_event": True,
        "sender": "",
        "forward_header": "",
        "forward_source": None,
        "reply_src_id": None,
        "reply_preview": "",
        "body_text": body_text,
        "body_entities": body_entities,
        "media": [],
        "media_note": "",
        "reactions": [],
    }


def extract_message_data(msg_div):
    classes = css_classes(msg_div)
    if "message" not in classes or "service" in classes:
        return None

    raw_id = msg_div.get("id", "")
    if not raw_id.startswith("message"):
        return None
    msg_id = raw_id[len("message"):]
    if not msg_id.isdigit():
        return None

    body = find_first(msg_div, "body")
    if body is None:
        body = msg_div

    date_div = None
    for el in body.iter():
        if el.tag == "div" and "date" in css_classes(el) and el.get("title"):
            date_div = el
            break
    timestamp = parse_export_date(date_div.get("title")) if date_div is not None else None

    sender = ""
    if config.settings.preserve_sender_names:
        for child in body:
            if isinstance(child.tag, str) and "from_name" in css_classes(child):
                sender = text_without(child, "date")
                break

    fwd_div = _forwarded_body(msg_div)
    forward_name = (
        sanitize_text(_forward_name(fwd_div)).replace("\n", " ").strip()
        if fwd_div is not None else ""
    )
    (forward_timestamp, forward_timestamp_has_time,
     forward_timestamp_has_seconds) = _forward_timestamp(fwd_div)
    # The source and original timestamp are rendered in a quoted attribution
    # header in the same reconstructed message/caption as the forwarded payload.
    forward_header = f"Forwarded from {forward_name}\n" if forward_name else ""
    fwd_src = extract_forward_source(msg_div) if config.settings.native_forward else None

    # A reply nested inside the forwarded payload describes the original
    # forwarded conversation. Only an outer message-level reply belongs to
    # the current export chat and may become a destination reply link.
    reply_div = _find_outer_reply_to(msg_div, fwd_div)
    preview = _reply_preview(reply_div)
    reply_src_id = _extract_reply_source_id(reply_div)

    # A reply preview inside the forwarded body belongs to the original
    # conversation. Preserve it separately from the outer chat's reply link.
    forward_reply_div = _find_forward_reply_to(fwd_div)
    forward_reply_preview = _reply_preview(forward_reply_div)
    forward_reply_src_id = _extract_reply_source_id(forward_reply_div)

    # For forwarded messages, read the payload text from the forwarded body
    # itself. Searching the whole message can select an unrelated outer text
    # block first and silently omit the forwarded caption.
    text_root = find_first(fwd_div, "text") if fwd_div is not None else None
    if text_root is None:
        text_root = find_first(msg_div, "text")
    body_text, body_entities, inline_stickers, missing_stickers = (
        parse_rich_text_with_stickers(text_root)
    )

    media = exportmedia.find_media(msg_div)
    sticker_emoji = exportmedia.sticker_emoji_text(msg_div)
    if sticker_emoji:
        body_text = (body_text + "\n\n" if body_text else "") + sticker_emoji
    missing_media = exportmedia.find_missing_media(msg_div)
    if missing_media:
        # Keep the message even when one or more referenced attachments are
        # absent. If other attachments exist, they are still uploaded normally.
        note = "\n".join(f"[Media not found: {reference}]" for reference in missing_media)
    else:
        note = "" if media else exportmedia.media_note(msg_div)
    if missing_stickers:
        sticker_note = "\n".join(
            f"[Sticker file not found: {reference}]"
            for reference in missing_stickers
        )
        note = (note + "\n" if note else "") + sticker_note
    if note:
        body_text = (body_text + "\n\n" if body_text else "") + note

    return {
        "msg_id": msg_id,
        "joined": "joined" in classes,
        "timestamp": timestamp,
        "sender": sender,
        "forward_header": forward_header,
        "forward_name": forward_name,
        "forward_timestamp": forward_timestamp,
        "forward_timestamp_has_time": forward_timestamp_has_time,
        "forward_timestamp_has_seconds": forward_timestamp_has_seconds,
        "is_forwarded": fwd_div is not None,
        "forward_source": fwd_src,
        "reply_src_id": reply_src_id,
        "reply_preview": preview,
        "forward_reply_src_id": forward_reply_src_id,
        "forward_reply_preview": forward_reply_preview,
        "body_text": body_text,
        "body_entities": body_entities,
        "media": media,
        "inline_stickers": inline_stickers,
        "media_note": note,
        "reactions": _extract_html_reactions(msg_div),
    }



def infer_media_groups(files, inline_emoji_ids=None):
    """Compatibility entry point for the album inference helper."""
    from .album import infer_media_groups as infer
    return infer(files, inline_emoji_ids)


def inherit_forwarded_sender(data, context):
    """Restore a forwarded author omitted from joined continuation messages."""
    if not data.get("is_forwarded"):
        context["last_forward_name"] = ""
    elif data.get("forward_name"):
        context["last_forward_name"] = data["forward_name"]
    elif data.get("joined") and context.get("last_forward_name"):
        data["forward_name"] = context["last_forward_name"]
        data["forward_header"] = f"Forwarded from {data['forward_name']}\n"
    else:
        context["last_forward_name"] = ""
