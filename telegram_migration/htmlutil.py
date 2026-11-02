"""Small helpers for walking the HTML of a Telegram Desktop export."""



def css_classes(el):
    return (el.get("class") or "").split()


def find_first(el, class_name):
    for child in el.iter():
        if child is not el and class_name in css_classes(child):
            return child
    return None


def inner_text(el):
    return "".join(el.itertext()).strip() if el is not None else ""


def text_without(el, skip_class):
    """Text of `el`, leaving out descendants that carry `skip_class`."""
    if el is None:
        return ""
    out = []

    def rec(e):
        if e.text:
            out.append(e.text)
        for c in e:
            if isinstance(c.tag, str) and skip_class not in css_classes(c):
                rec(c)
            if c.tail:
                out.append(c.tail)

    rec(el)
    return "".join(out).strip()
