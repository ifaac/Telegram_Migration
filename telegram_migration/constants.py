"""Fixed values that are part of how Telegram and the export work (not user settings)."""

# Telegram's hard limit for the text of one message (every account, Premium included).
TEXT_LIMIT = 4096

# The largest caption any account can send (Premium user accounts); bots are limited to 1024.
MAX_CAPTION_LIMIT = 2048

# The caption limit this tool can actually use: the script authenticates with a bot token,
# and bot accounts cannot be Premium, so 1024 is the effective ceiling.
BOT_CAPTION_LIMIT = 1024

# The smallest text_limit or caption_limit that makes sense. Below 4, the splitter's
# surrogate-pair avoidance can produce a chunk that exceeds the requested limit by one
# UTF-16 unit; Settings.validate() rejects such values so the splitter never faces that
# choice through the public API.
MIN_TEXT_LIMIT = 4

# messages.html, messages2.html, ... in a Telegram Desktop HTML export.
HTML_FILE_PATTERN = r"^messages(\d*)\.html$"

# Media types whose sender/message context is kept in a separate text post,
# with the media replying to it (the original wheel's fallback).
STICKER_KINDS = ("sticker_static", "sticker_animated", "sticker_video", "sticker_unknown")
NO_CAPTION_KINDS = ("voice", "round_video", *STICKER_KINDS)
