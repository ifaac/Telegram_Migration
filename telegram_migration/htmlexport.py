"""Read one message out of a Telegram Desktop HTML export."""

import os.
import re
from datetime import datetime
from urllib.parse import parse_qs
from urllib.parse import urlparse

from . import config, exportmedia
from .htmlutil import css_classes, find_first, inner_text, text_without
from .richtext import parse_rich_text
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
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _forward_name(fwd_div):
    if fwd_div is None:
        return ""
    a = next(fwd_div.iter("a"), None)
    if a is not None and inner_text(a):
        return inner_text(a)
    return text_without(find_first(fwd_div, "from_name"), "date")


def extract_forward_source(msg_div):
    fwd_div = find_first(msg_div, "forwarded")
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


def _extract_reply_source_id(msg_div):
    reply_div = find_first(msg_div, "reply_to")
    if reply_div is None:
        return None
    a = next(reply_div.iter("a"), None)
    if a is None:
        return None
    match = re.search(r"#go_to_message(\d+)", a.get("href") or "")
    return match.group(1) if match else None


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

    fwd_div = find_first(msg_div, "forwarded")
    forward_header = ""
    if fwd_div is not None:
        name = _forward_name(fwd_div)
        if name:
            forward_header = f"Forwarded from {name}\n\n"
    fwd_src = extract_forward_source(msg_div) if config.settings.native_forward else None

    reply_div = find_first(msg_div, "reply_to")
    preview = _reply_preview(reply_div)
    reply_src_id = _extract_reply_source_id(msg_div)

    text_div = find_first(msg_div, "text")
    body_text, body_entities = parse_rich_text(text_div)

    media = exportmedia.find_media(msg_div)
    note = "" if media else exportmedia.media_note(msg_div)
    if note:
        body_text = (body_text + "\n\n" if body_text else "") + note

    return {
        "msg_id": msg_id,
        "joined": "joined" in classes,
        "timestamp": timestamp,
        "sender": sender,
        "forward_header": forward_header,
        "forward_source": fwd_src,
        "reply_src_id": reply_src_id,
        "reply_preview": preview,
        "body_text": body_text,
        "body_entities": body_entities,
        "media": media,
        "media_note": note,
        "reactions": _extract_html_reactions(msg_div),
    }
