"""Text and entity helpers. All offsets and lengths are UTF-16 code units, as Telegram expects."""

import copy
import re

from telethon.tl import types as tl


_EMOJI_BASE_RANGES = (
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x3299),
    (0x1F000, 0x1FAFF), (0x1FC00, 0x1FFFD),
)
_EMOJI_IGNORABLE = {
    0x200D, 0xFE0E, 0xFE0F, 0x20E3,
    *range(0x1F3FB, 0x1F400), *range(0xE0020, 0xE0080),
}


def is_emoji_only_text(value):
    """Return True only when non-whitespace content consists entirely of emoji.

    Handles common emoji presentation selectors, ZWJ sequences, skin tones,
    regional-indicator flags and keycap sequences without adding a runtime
    dependency. It is intentionally conservative: ordinary digits, symbols,
    punctuation and mixed text are not classified as emoji-only.
    """
    value = str(value or "").strip()
    if not value:
        return False

    has_emoji = False
    index = 0
    while index < len(value):
        char = value[index]
        codepoint = ord(char)

        if char.isspace() or codepoint in _EMOJI_IGNORABLE:
            index += 1
            continue

        # Keycaps are the only emoji sequences whose base is ASCII.
        if char in "#*0123456789":
            end = index + 1
            if end < len(value) and ord(value[end]) == 0xFE0F:
                end += 1
            if end < len(value) and ord(value[end]) == 0x20E3:
                has_emoji = True
                index = end + 1
                continue
            return False

        # Regional indicator pairs form flag emoji. A lone regional indicator
        # is not accepted as a complete visible emoji.
        if 0x1F1E6 <= codepoint <= 0x1F1FF:
            if index + 1 >= len(value) or not (
                0x1F1E6 <= ord(value[index + 1]) <= 0x1F1FF
            ):
                return False
            has_emoji = True
            index += 2
            continue

        if any(start <= codepoint <= end for start, end in _EMOJI_BASE_RANGES):
            has_emoji = True
            index += 1
            continue

        return False

    return has_emoji


def u16len(s):
    """Length in UTF-16 code units (Telegram's unit for entity offsets)."""
    return len(s.encode("utf-16-le")) // 2


def _clip_entities(entities, a, b):
    """Entities restricted to [a, b) and re-based so that `a` becomes 0."""
    out = []
    for e in entities:
        s, t = max(e.offset, a), min(e.offset + e.length, b)
        if t > s:
            e2 = copy.copy(e)
            e2.offset = s - a
            e2.length = t - s
            out.append(e2)
    return out


def strip_with_entities(text, entities):
    """Strip leading/trailing whitespace and keep entities aligned.

    The leading offset is computed in UTF-16 code units (Telegram's unit)
    rather than Python codepoints. For ASCII/BMP whitespace the two are
    identical, but measuring in UTF-16 avoids an off-by-N shift if the
    composed message ever starts with a non-BMP codepoint.
    """
    stripped = text.lstrip()
    lead = u16len(text) - u16len(stripped)
    stripped = stripped.rstrip()
    if lead == 0 and len(stripped) == len(text):
        return text, entities
    return stripped, _clip_entities(entities, lead, lead + u16len(stripped))


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(s):
    """Normalise text BEFORE entity offsets are measured.

    Carriage returns become newlines and control characters are removed, so the
    text Telegram receives is exactly the text the offsets were computed on.
    """
    if not s:
        return s
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARS_RE.sub("", s)


def _shift(e, shift):
    if shift == 0:
        return e
    e2 = copy.copy(e)
    e2.offset = e.offset + shift
    return e2


_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def format_archive_timestamp(timestamp, date_only=False):
    """Format an exported timestamp for the readable Telegram archive header.

    Month names are deliberately locale-independent so the same export gets
    the same display on Windows, Linux, and Google Colab.
    """
    if timestamp is None:
        return ""
    day = f"{timestamp.day} {_MONTH_NAMES[timestamp.month - 1]} {timestamp.year}"
    if date_only:
        return day
    return f"{day} · {timestamp:%H:%M}"


def compose_message_header(timestamp, sender, date_only=False):
    """Build a readable sender/date header with Telegram formatting entities.

    The sender is bold and the timestamp italic. Both offsets and lengths use
    UTF-16 code units, matching Telegram's entity format. The returned header
    ends with a blank line so the message body starts as a separate paragraph.
    """
    text = ""
    entities = []

    label = sanitize_text(str(sender or "")).replace("\n", " ").strip()
    if label:
        start = u16len(text)
        text += label
        length = u16len(label)
        if length:
            entities.append(tl.MessageEntityBold(offset=start, length=length))
        text += "\n"

    stamp = format_archive_timestamp(timestamp, date_only=date_only)
    if stamp:
        start = u16len(text)
        text += stamp
        length = u16len(stamp)
        if length:
            entities.append(tl.MessageEntityItalic(offset=start, length=length))
        text += "\n\n"
    elif label:
        text += "\n"

    return text, entities


def compose_message(watermark, sender, forward, body_text, body_entities, extra_before_body=None):
    text, entities = "", []
    parts = [watermark, sender, forward]
    if extra_before_body:
        parts.append(extra_before_body)
    parts.append((body_text, body_entities))
    for part, part_ents in parts:
        part = sanitize_text(part)
        if not part:
            continue
        base = u16len(text)
        text += part
        for e in part_ents:
            entities.append(_shift(e, base))
    return text, entities


def split_with_entities(text, entities, first_limit, limit):
    """Split text into chunks no larger than the limits.

    Whitespace is preserved at chunk boundaries: the whole input is stripped
    once at the top, then continuation chunks are NOT individually stripped,
    so ``"".join(piece for piece, _ in chunks) == stripped_text``.

    Surrogate pairs are never split across chunks. If a limit of 1 lands on a
    non-BMP character, that one chunk exceeds the limit by one UTF-16 unit
    rather than looping forever (a defensive guard; config rejects limits
    below 4, so this cannot happen through the public API).
    """
    if first_limit < 1 or limit < 1:
        raise ValueError(
            f"split limits must be at least 1 (got first_limit={first_limit}, limit={limit})"
        )
    text, entities = strip_with_entities(text, entities)
    b = text.encode("utf-16-le")
    n = len(b) // 2
    if n <= first_limit:
        return [(text, entities)]

    def unit(i):
        return b[2 * i] | (b[2 * i + 1] << 8)

    custom_emoji_ranges = [
        (int(entity.offset), int(entity.offset + entity.length))
        for entity in entities
        if type(entity).__name__ == "MessageEntityCustomEmoji"
    ]

    chunks, start, lim = [], 0, first_limit
    while start < n:
        hard = min(n, start + lim)
        if hard == n:
            end = nxt = n
        else:
            end = nxt = hard
            lo = start + (lim * 3) // 4
            for i in range(hard - 1, lo - 1, -1):
                if unit(i) in (0x0A, 0x20):
                    end = nxt = i + 1     # keep the whitespace in this chunk
                    break
            else:
                if 0xD800 <= unit(hard - 1) <= 0xDBFF:
                    end = nxt = hard - 1
        # A custom emoji entity must cover its entire fallback emoji in
        # one Telegram message. Never split its UTF-16 range between chunks.
        for entity_start, entity_end in custom_emoji_ranges:
            if entity_start < nxt < entity_end:
                if entity_start > start:
                    end = nxt = entity_start
                else:
                    # A single emoji may be longer than a deliberately tiny
                    # configured limit. Keep it intact in one slightly larger
                    # chunk rather than sending a broken custom-emoji entity.
                    end = nxt = entity_end
                break

        if nxt <= start:
            # Forward-progress guard: a surrogate pair at the boundary with
            # lim == 1 would otherwise leave nxt == start and loop forever.
            end = nxt = min(start + 2, n)
        seg = b[2 * start:2 * end].decode("utf-16-le")
        ents = _clip_entities(entities, start, end)
        if seg:
            chunks.append((seg, ents))
        start, lim = nxt, limit
    return chunks or [(text, entities)]


def clean_invalid_entities(text, entities):
    """Drop or clip entities whose bounds are invalid for `text`.

    Telegram rejects a message outright with "Some of provided entities have
    invalid bounds" when any entity has offset < 0, length <= 0, or
    offset + length > UTF-16 length of the text. This helper enforces those
    three invariants:

      1. entities entirely outside the text range     -> discarded
      2. entities that spill past the end of the text -> length shrunk
      3. entities with zero or negative length         -> discarded

    Valid entities pass through unchanged.
    """
    if not entities:
        return entities
    text_len = u16len(text or "")
    out = []
    for e in entities:
        offset = getattr(e, "offset", None)
        length = getattr(e, "length", None)
        if not isinstance(offset, int) or not isinstance(length, int):
            continue
        if length <= 0:
            continue
        if offset < 0 or offset >= text_len:
            continue
        if offset + length > text_len:
            e2 = copy.copy(e)
            e2.length = text_len - offset
            if e2.length <= 0:
                continue
            out.append(e2)
        else:
            out.append(e)
    return out
