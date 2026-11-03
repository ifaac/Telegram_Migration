"""Text and entity helpers. All offsets and lengths are UTF-16 code units, as Telegram expects."""

import copy.
import re


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
    b = text.encode("utf-16-le")
    n = len(b) // 2
    if n <= first_limit:
        return [(text, entities)]

    def unit(i):
        return b[2 * i] | (b[2 * i + 1] << 8)

    chunks, start, lim = [], 0, first_limit
    while start < n:
        hard = min(n, start + lim)
        if hard == n:
            end = nxt = n
        else:
            end = nxt = hard
            lo = start + (lim * 3) // 4
            for i in range(hard - 1, lo - 1, -1):      # last whitespace in the final quarter
                if unit(i) in (0x0A, 0x20):
                    end, nxt = i, i + 1
                    break
            else:
                if 0xD800 <= unit(hard - 1) <= 0xDBFF:  # do not split a surrogate pair
                    end = nxt = hard - 1
        seg = b[2 * start:2 * end].decode("utf-16-le")
        ents = _clip_entities(entities, start, end)
        seg, ents = strip_with_entities(seg, ents)
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
