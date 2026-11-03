# Telegram Desktop Chat-History Migration (v9.0)

A Python tool for reconstructing a Telegram conversation in a destination channel from a **Telegram Desktop export** (`messages.html`, `messages2.html`, etc.) and the media files that accompany it. Use it as a **command-line tool** (`python -m telegram_migration`) or import it as a **Python library** (`from telegram_migration import Settings, run`).

> **Scope:** this program migrates a local export. It does not sign into Telegram Web or retrieve the complete live history of an unrelated channel. Most reconstructed messages are newly sent by your destination bot; the program cannot recreate arbitrary historical sender identities or original native reaction totals. This program uses Telegram's official Bot API/MTProto bot authentication to send migrated messages, rather than logging in through your personal Telegram account. It does not require your personal phone number, SMS login code, or personal account session, reducing the exposure of your personal account credentials. The bot operates under its own identity, so sending limits and restrictions primarily affect the bot.

## Features

- Streams Telegram Desktop HTML exports instead of loading every page into memory at once.
- Reconstructs supported rich-text formatting (including italics, bold, underline, strike, code, links, spoilers, and blockquotes) using Telegram message entities.
- Preserves sender names and original timestamps as archival text where available.
- Handles common exported media: photos, documents, videos, GIFs, voice messages, round videos, and sticker files.
- Maps source message IDs to destination IDs to restore reply links where possible; audits available reply links.
- Persists partial-send state so interrupted runs can often resume without intentionally resending already-recorded pieces.
- Checks oversized attachments before sending and stops with instructions for manual handling.
- Uses FloodWait handling and records failures and formatting fallbacks.
- Can archive reaction metadata from a compatible companion JSON export as **text notes**. It cannot restore the original native reaction bar, original voters, or historical reaction counts as live Telegram reactions.
- Uses a Telegram bot session for sending. It does not require a personal-account login for the migration itself.

## Requirements

- Python 3.10 or newer recommended.
- A Telegram API ID and API hash from [my.telegram.org](https://my.telegram.org).
- A bot token from [@BotFather](https://t.me/BotFather).
- The bot must be an administrator in the destination channel with permission to post.
- A Telegram Desktop export with its HTML pages and media folders kept together.

### Install

```bash
python -m venv .venv
```

Activate the virtual environment, then install the package from a clone of this repository:

```bash
python -m pip install .
```

or straight from GitHub:

```bash
python -m pip install git+https://github.com/ifaac/Telegram_Migration
```

For development (run from the repository root, no install needed) use `python -m pip install -r requirements.txt`.

`static-ffmpeg` is an optional helper for automatic FFmpeg/ffprobe setup when a video sticker needs metadata (`python -m pip install ".[media]"`). The program attempts a lazy install only if it is needed and no working system binaries are available.

## Configure

1. Copy `.env.example` to `.env`.
2. Replace the example API hash, bot token, and destination peer with your own values.
3. Put your exported Telegram HTML pages and media folders in the directory specified by `TELEGRAM_EXPORT_DIR`.

Example `.env`:

```dotenv
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=replace_with_your_api_hash
TELEGRAM_BOT_TOKEN=replace_with_your_new_bot_token
TELEGRAM_CHANNEL_PEER=-1001234567890
TELEGRAM_EXPORT_DIR=./telegram_export
TELEGRAM_DRY_RUN=true
TELEGRAM_SKIP_CONFIRMATION=false
```

`TELEGRAM_CHANNEL_PEER` may be a numeric peer ID (for example `-1001234567890`) or a public `@username`. The program intentionally has no preconfigured destination ID.

`TELEGRAM_API_ID` has no default: use your own from [my.telegram.org](https://my.telegram.org). The program refuses to start while any credential is missing or still an example value.

Optional settings (defaults in brackets):

| Variable | Meaning |
| --- | --- |
| `TELEGRAM_MAX_UPLOAD_MB` [`2000`] | Stop at any message with a file larger than this many MiB (`0` disables the check). 2000 MiB is believed to be Telegram's MTProto ceiling; verify it with your bot. |
| `TELEGRAM_BATCH_LIMIT` [`1000`] | Stop after this many messages (sent + failed). |
| `TELEGRAM_SEND_DELAY` [`2.5`] | Seconds to wait between messages. |
| `TELEGRAM_SESSION_NAME` [`colab_bot_restorer_session`] | Name of the Telethon session file. |


### Google Colab

Colab Secrets named `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHANNEL_PEER` are read automatically (enable notebook access for each). Run the migration **inside the notebook** so it can read the secrets (a `!python ...` command starts a separate process that cannot):

```python
!pip install -q git+https://github.com/ifaac/Telegram_Migration
```

```python
from telegram_migration import Settings, run

settings = Settings.from_env(
    base_dir="/content/drive/MyDrive/ChatExport_2025-01-01",   # your export folder
    dry_run=True,                                                # preview first
)
run(settings)
```

When the preview looks right, run the cell again with `dry_run=False`.

The program mounts Google Drive automatically when the export directory is under `/content/drive/` and Drive is not mounted yet.

## Run safely

**Dry-run is enabled by default.** Start by running:

```bash
python -m telegram_migration
```

(`telegram-migration` does the same once the package is installed.) Review the reported source file count, destination channel title/ID, and dry-run summary. No messages are sent in a dry run.

When you are satisfied with the preview, either set `TELEGRAM_DRY_RUN=false` in `.env` or pass `--send`:

```bash
python -m telegram_migration --send
```

The program asks you to confirm the target before sending; keep `TELEGRAM_SKIP_CONFIRMATION=false` (or omit `--yes`) so it does.

Command-line options override the environment (`python -m telegram_migration --help` lists them):

| Option | Effect |
| --- | --- |
| `--export-dir PATH` | Folder with `messages.html` and the media folders. |
| `--channel PEER` | Destination: numeric peer ID or `@username`. |
| `--dry-run` / `--send` | Preview only / really send. |
| `--yes` | Skip the confirmation prompt. |
| `--max-upload-mb N` | Stop at files larger than N MiB (`0` disables). |
| `--batch-limit N`, `--delay SECONDS` | Stop after N messages / wait between messages. |
| `--env-file PATH` | Load this `.env` file instead of `./.env`. |

## Use as a Python library

```python
from telegram_migration import Settings, run

settings = Settings.from_env()      # TELEGRAM_* variables, .env file, Colab Secrets
run(settings)                       # or: run(dry_run=False)
```

`Settings` is a plain dataclass, so everything can also be given directly (`Settings(api_id=..., base_dir=..., max_upload_mb=500)`). `run()` works from scripts and notebooks; `await telegram_migration.run_async(settings)` is the coroutine version. Async runs scope settings to their task, so concurrent migrations do not overwrite each other's configuration. Importing the package reads no environment variables and touches no files; credentials problems raise `telegram_migration.ConfigError`.

Telegram's per-message and caption limits default to 4096 and 1024 UTF-16 units. Set `text_limit` and `caption_limit` on `Settings` when the destination account supports different limits (for example, `caption_limit=2048` for Premium).

## Progress files and resuming

The script keeps its progress files inside the export directory. These include the ID map, partial-send state, pending replies, failure log, manual-upload list, formatting audit, and reply audit. Keep these files between runs if you want resumability. Do not commit them to a public repository: they can reveal private chat metadata.

If the program stops at a file larger than the configured upload ceiling, follow the printed manual-upload instructions **before** running the program again. The script can mark that source message to be skipped on the next run.

## Project layout

The code is a small package instead of one long script:

| Module | Responsibility |
| --- | --- |
| `config` | The `Settings` dataclass, environment/`.env`/Colab-Secrets loading, validation. |
| `pipeline` | The run itself: stream the export, send each message, resume, stop cleanly. |
| `htmlexport`, `richtext`, `htmlutil` | Read a message out of the HTML export; turn its rich text into Telegram entities. |
| `exportmedia`, `mediatools` | Find and classify media; optional ffmpeg/hachoir helpers. |
| `sending`, `uploads`, `formatfallback` | Send text/albums/voice/stickers/forwards; upload files; retry without formatting if Telegram rejects it. |
| `progress`, `replylinks`, `reaction_archive` | ID map and resume state; reply verification; reaction notes. |
| `bigfiles` | Oversize-file check, manual-upload list and stop instructions. |
| `textutil`, `constants` | UTF-16 text/entity helpers; fixed Telegram limits. |
| `cli`, `runner` | Command line and the synchronous `run()`. |

## Known limitations

- Telegram service messages (for example, join/leave events and title changes) are intentionally excluded.
- The destination messages are not identical to the original Telegram records. Sender names and timestamps are represented as text in reconstructed messages unless a native forward is possible.
- The source HTML and media folder must contain the required data. A missing attachment cannot be recreated by this program.
- If Telegram rejects formatting entities, the script can fall back to plain text for that message; check `migration_formatting_dropped.jsonl`.
- Reaction archives are informational text notes, not native Telegram reactions.
- Native forwards may be unavailable for source messages whose origins are inaccessible; the program's configured forwarding fallback may use a textual header.
- Files above the upload limit (`max_upload_mb`) cause a clean stop and require manual handling. Files below the configured limit can still fail due to network, server, permissions, or media constraints.

## Development checks

The repository includes an offline regression suite. The tests do not sign in to Telegram, contact Telegram servers, send messages, or require API credentials.

Run the tests from the repository root:

```bash
python -m pip install -r requirements.txt
python -m compileall -q telegram_migration
python -m unittest discover -s tests -v
```

The suite covers rich-text parsing and UTF-16 entity offsets, long-text chunking, export parsing, dates and replies, media classification, GIF/sticker upload options, upload-size checks, progress and ID-map persistence, manual-upload records, reaction metadata, settings and the command line, the module structure (no undefined names), and complete runs against a fake Telegram client: dry run, resume, deferred replies, formatting fallback, and the oversize stop. The fake clients cannot post to a real Telegram chat.

## Security

- Never commit `.env`, bot tokens, API hashes, Telegram session files, exports, media, or migration state.
- If you have ever exposed a bot token, revoke it with BotFather and replace it before running the program.
- Treat API hashes and Telethon session files as private credentials.
- Avoid sharing migration logs publicly; they may contain source message IDs and message-specific error details.

## License

GNU General Public License v3.0
