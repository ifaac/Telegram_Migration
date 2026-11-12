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

# Exported sticker assets are sent individually as ordinary documents, not
# native stickers, so their sender/date attribution can be carried as a caption.
STICKER_KINDS = ("sticker_static", "sticker_animated", "sticker_video", "sticker_unknown")
# Every currently supported media type has a caption-capable send path:
# voice notes use voice_note=True; sticker assets use ordinary document uploads;
# captioned round videos are uploaded as regular videos.
NO_CAPTION_KINDS = ()
