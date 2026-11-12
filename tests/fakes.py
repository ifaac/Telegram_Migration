"""Shared test doubles: a fake Telegram client and a tiny Telegram Desktop export builder.

Nothing here talks to Telegram or needs credentials.
"""
import os
from types import SimpleNamespace


class FakeMessage:
    def __init__(self, message_id, grouped_id=None, message=""):
        self.id = message_id
        self.grouped_id = grouped_id
        self.message = message
        self.text = message


class FakeTelegram:
    """Stands in for telethon.TelegramClient; records every call in `.calls`."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.next_id = 1000
        self.next_grouped_id = -1
        self.messages = {}
        self.disconnected = False
        self.reject_formatting = None       # set to an Exception to reject any message with entities
        FakeTelegram.instances.append(self)

    def _new_id(self):
        self.next_id += 1
        return self.next_id

    async def start(self, **kwargs):
        return self

    async def get_entity(self, peer):
        return SimpleNamespace(
            title="Test channel", id=1, access_hash=111,
            username=None, broadcast=True, megagroup=False, noforwards=False,
        )

    async def __call__(self, request):
        from telethon.tl import functions as fn

        if isinstance(request, fn.channels.GetFullChannelRequest):
            self.calls.append(("get_full_channel", request))
            return SimpleNamespace(
                full_chat=SimpleNamespace(linked_chat_id=2),
                chats=[SimpleNamespace(id=2, access_hash=222)],
            )
        if isinstance(request, fn.messages.SendMessageRequest):
            self.calls.append(("send_discussion_reply", request))
            message = FakeMessage(self._new_id(), message=request.message)
            self.messages[message.id] = message
            return SimpleNamespace(updates=[
                SimpleNamespace(random_id=request.random_id, id=message.id)
            ])
        raise AssertionError(f"Unexpected Telegram request in fake: {type(request).__name__}")

    async def disconnect(self):
        self.disconnected = True

    async def get_input_entity(self, user_id):
        raise ValueError("unknown user")

    async def forward_messages(self, *args, **kwargs):
        raise ValueError("forwarding is not available in tests")

    async def get_messages(self, channel, ids=None):
        self.calls.append(("get_messages", ids))
        if isinstance(ids, (list, tuple)):
            return [self.messages.get(int(message_id)) for message_id in ids]
        return self.messages.get(int(ids)) if ids is not None else None

    async def edit_message(self, channel, message, text, **kwargs):
        message_id = message.id if hasattr(message, "id") else int(message)
        existing = self.messages.get(message_id)
        if existing is None:
            return None
        existing.message = text
        existing.text = text
        self.calls.append(("edit_message", message_id, text, kwargs))
        return existing

    async def send_message(self, channel, text, **kwargs):
        if self.reject_formatting and kwargs.get("formatting_entities"):
            raise self.reject_formatting
        self.calls.append(("send_message", text, kwargs))
        message = FakeMessage(self._new_id(), message=text)
        self.messages[message.id] = message
        return message

    async def send_file(self, channel, files, **kwargs):
        self.calls.append(("send_file", files, kwargs))
        captions = kwargs.get("caption") or ""
        if isinstance(files, list):
            if not isinstance(captions, (list, tuple)):
                captions = [captions] * len(files)
            grouped_id = self.next_grouped_id
            self.next_grouped_id -= 1
            messages = [
                FakeMessage(self._new_id(), grouped_id=grouped_id,
                            message=captions[index] if index < len(captions) else "")
                for index, _file in enumerate(files)
            ]
            for message in messages:
                self.messages[message.id] = message
            return messages
        message = FakeMessage(
            self._new_id(),
            message=captions if isinstance(captions, str) else (captions[0] if captions else ""),
        )
        self.messages[message.id] = message
        return message

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
