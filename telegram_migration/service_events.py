"""Parse Telegram service-event references and timestamps."""

import re

from .htmlutil import css_classes, find_first, inner_text
from .htmlexport import extract_service_event_timestamp
from .richtext import parse_rich_text


def extract_pinned_message_id(msg_div):
    """Return the source message ID linked by a Telegram pin service event."""
    classes = css_classes(msg_div)
    raw_id = msg_div.get("id", "")
    if "message" not in classes or "service" not in classes:
        return None
    if not re.fullmatch(r"message\d+", raw_id):
        return None
    for anchor in msg_div.iter("a"):
        match = re.fullmatch(r"#go_to_message(\d+)", anchor.get("href") or "")
        if match:
            return match.group(1)
    return None


def extract_pin_event_timestamp(msg_div, current_date=None):
    """Backward-compatible timestamp accessor for the pin audit report."""
    timestamp, _date_only = extract_service_event_timestamp(msg_div, current_date)
    return timestamp


def extract_service_event_data(msg_div, current_date=None, *, pinned_message_id=None):
    """Extract a service event with its own timestamp when the HTML provides one.

    The surrounding date divider is used only as a fallback. The event body
    is parsed without removing or relocating any HTML elements.
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

    timestamp, timestamp_date_only = extract_service_event_timestamp(msg_div, current_date)
    return {
        "msg_id": match.group(1),
        "joined": False,
        "timestamp": timestamp,
        "timestamp_date_only": timestamp_date_only,
        "service_event": True,
        "sender": "",
        "forward_header": "",
        "forward_source": None,
        "reply_src_id": str(pinned_message_id) if pinned_message_id else None,
        "reply_preview": "",
        "body_text": body_text,
        "body_entities": body_entities,
        "media": [],
        "media_note": "",
        "reactions": [],
    }
