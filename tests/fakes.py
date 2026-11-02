"""Shared test doubles: a fake Telegram client and a tiny Telegram Desktop export builder.

Nothing here talks to Telegram or needs credentials.
"""
import os
from types import SimpleNamespace


class FakeMessage:
    def __init__(self, message_id):
        self.id = message_id


class FakeTelegram:
    """Stands in for telethon.TelegramClient; records every call in `.calls`."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.next_id = 1000
        self.disconnected = False
        self.reject_formatting = None       # set to an Exception to reject any message with entities
        FakeTelegram.instances.append(self)

    def _new_id(self):
        self.next_id += 1
        return self.next_id

    async def start(self, **kwargs):
        return self

    async def get_entity(self, peer):
        return SimpleNamespace(title="Test channel", id=1, username=None)

    async def disconnect(self):
        self.disconnected = True

    async def get_input_entity(self, user_id):
        raise ValueError("unknown user")

    async def forward_messages(self, *args, **kwargs):
        raise ValueError("forwarding is not available in tests")

    async def get_messages(self, channel, ids=None):
        self.calls.append(("get_messages", ids))
        return []

    async def send_message(self, channel, text, **kwargs):
        if self.reject_formatting and kwargs.get("formatting_entities"):
            raise self.reject_formatting
        self.calls.append(("send_message", text, kwargs))
        return FakeMessage(self._new_id())

    async def send_file(self, channel, files, **kwargs):
        self.calls.append(("send_file", files, kwargs))
        if isinstance(files, list):
            return [FakeMessage(self._new_id()) for _ in files]
        return FakeMessage(self._new_id())

    # convenience --------------------------------------------------------------------------
    def texts(self):
        return [c[1] for c in self.calls if c[0] == "send_message"]

    def kinds(self, name):
        return [c for c in self.calls if c[0] == name]


# --- export builder ------------------------------------------------------------------------
def message(msg_id, inner="", *, sender="Alice", classes="", minute=None):
    minute = msg_id if minute is None else minute
    sender_html = f'<div class="from_name">{sender}</div>' if sender else ""
    return (
        f'<div class="message default clearfix {classes}" id="message{msg_id}"><div class="body">'
        f'<div class="pull_right date details" title="12.03.2023 10:{minute:02d}:00 UTC+02:00">t</div>'
        f"{sender_html}{inner}</div></div>"
    )


def text(value):
    return f'<div class="text">{value}</div>'


def reply_to(target_id):
    return f'<div class="reply_to details">In reply to <a href="#go_to_message{target_id}">this message</a></div>'


def video(relative_path):
    return (f'<div class="media_wrap clearfix"><a class="video_file_wrap clearfix pull_left" '
            f'href="{relative_path}"><div class="video_play_bg"></div></a></div>')


def photo(relative_path):
    return (f'<div class="media_wrap clearfix"><a class="photo_wrap clearfix pull_left" '
            f'href="{relative_path}"><img src="{relative_path}"></a></div>')


def forwarded(name, body):
    return (f'<div class="forwarded body"><div class="from_name">{name} '
            f'<span class="date details" title="x">01.01.2023</span></div>{text(body)}</div>')


def write_export(folder, messages, name="messages.html"):
    with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
        handle.write("<html><body><div class='history'>" + "".join(messages) + "</div></body></html>")


def write_file(folder, relative_path, size=16):
    path = os.path.join(folder, relative_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size)               # sparse where the filesystem supports it
    return path
