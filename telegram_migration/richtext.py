"""Convert the rich text of an exported message (HTML) into text plus Telegram entities."""

from urllib.parse import urlparse

from telethon.tl import types as tl

from .htmlutil import css_classes
from .textutil import sanitize_text, strip_with_entities, u16len


_URL_SCHEMES = ("http", "https", "tg", "ftp", "ftps")


def _make_entity(name, **kwargs):
    cls = getattr(tl, name, None)
    if cls is None:
        return None
    try:
        return cls(**kwargs)
    except TypeError:
        kwargs.pop("collapsed", None)        # older Telethon: no `collapsed`
        try:
            return cls(**kwargs)
        except TypeError:
            return None


def parse_rich_text(root):
    if root is None:
        return "", []

    parts, entities, pos = [], [], [0]
    pre_depth = [0]

    def add(s):
        s = sanitize_text(s)
        if s:
            parts.append(s)
            pos[0] += u16len(s)

    def emit(name, start, end, **extra):
        length = end - start
        if length > 0:
            e = _make_entity(name, offset=start, length=length, **extra)
            if e is not None:
                entities.append(e)

    def span(child, name, **extra):
        start = pos[0]
        walk(child)
        emit(name, start, pos[0], **extra)

    def walk(el):
        if el.text:
            add(el.text)
        for child in el:
            tag = child.tag if isinstance(child.tag, str) else ""
            cls = css_classes(child) if tag else []

            if not tag:
                pass                                   # comment / processing instruction

            elif tag == "br":
                add("\n")

            elif tag == "i" and "emoji" in cls:
                add("".join(child.itertext()))

            elif tag == "a" and child.get("href"):
                href = child.get("href")
                start = pos[0]
                walk(child)
                end = pos[0]
                if href.startswith("tg://user?id="):
                    try:
                        uid = int(href.split("id=", 1)[1])
                        emit("MessageEntityMentionName", start, end, user_id=uid)
                    except (ValueError, IndexError):
                        pass
                elif href.startswith("mailto:"):
                    emit("MessageEntityEmail", start, end)
                elif urlparse(href).scheme.lower() in _URL_SCHEMES:
                    emit("MessageEntityTextUrl", start, end, url=href)

            elif tag in ("b", "strong"):
                span(child, "MessageEntityBold")

            elif tag in ("i", "em"):
                span(child, "MessageEntityItalic")

            elif tag in ("u", "ins"):
                span(child, "MessageEntityUnderline")

            elif tag in ("s", "strike", "del"):
                span(child, "MessageEntityStrike")

            elif tag == "code":
                if pre_depth[0]:
                    walk(child)                        # already inside <pre>: no second entity
                else:
                    span(child, "MessageEntityCode")

            elif tag == "pre":
                inner = next((c for c in child if c.tag == "code"), None)
                lang = ""
                if inner is not None:
                    for c in css_classes(inner):
                        if c.startswith("language-"):
                            lang = c[len("language-"):]
                            break
                pre_depth[0] += 1
                try:
                    span(child, "MessageEntityPre", language=lang)
                finally:
                    pre_depth[0] -= 1

            elif tag == "blockquote":
                span(child, "MessageEntityBlockquote", collapsed=False)

            elif tag == "tg-spoiler" or (tag == "span" and ("spoiler" in cls or "tg-spoiler" in cls)):
                span(child, "MessageEntitySpoiler")

            else:
                walk(child)

            if child.tail:
                add(child.tail)

    walk(root)
    return strip_with_entities("".join(parts), entities)
