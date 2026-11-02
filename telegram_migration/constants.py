"""Fixed values that are part of how Telegram and the export work (not user settings)."""

# Telegram's hard limit for the text of one message.
TEXT_LIMIT = 4096

# messages.html, messages2.html, ... in a Telegram Desktop HTML export.
HTML_FILE_PATTERN = r"^messages(\d*)\.html$"

# Sticker files need sticker-specific upload attributes and cannot be captioned like
# ordinary photos/videos, so their text is sent as a separate message first.
STICKER_KINDS = ("sticker_static", "sticker_animated", "sticker_video", "sticker_unknown")
NO_CAPTION_KINDS = ("voice", "round_video", *STICKER_KINDS)
