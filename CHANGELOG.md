# Changelog

## v9.0.0

The migration itself behaves exactly as in v8.2; what changed is how the code is packaged and configured.

**Breaking**

- The single 2,600-line `telegram_migration.py` is now the `telegram_migration/` package (no module over about 370 lines). Run it with `python -m telegram_migration` (or `telegram-migration` after `pip install .`) or use it as a library: `from telegram_migration import Settings, run`.
- `%run telegram_migration.py` no longer exists. In Colab, call `run()` inside the notebook (see the README).
- Configuration is a `Settings` object instead of constants at the top of a file. It is still read from the `TELEGRAM_*` environment variables, a `.env` file and Colab Secrets (now including `TELEGRAM_API_ID`).
- Importing the package has no side effects: it no longer loads `.env`, reads the environment or starts a run. The old Colab/Jupyter auto-start on import is gone.
- Missing Telethon or lxml are no longer installed automatically at import; install the package with pip. (The small optional helpers for hachoir, ffmpeg and nest_asyncio are still installed on demand.)
- Missing credentials raise `ConfigError` (exit status 2 on the command line), and the example values from `.env.example` are rejected.

**Added**

- Command-line options: `--export-dir`, `--channel`, `--dry-run`/`--send`, `--yes`, `--max-upload-mb`, `--batch-limit`, `--delay`, `--env-file`, `--version`.
- Optional variables `TELEGRAM_MAX_UPLOAD_MB`, `TELEGRAM_BATCH_LIMIT`, `TELEGRAM_SEND_DELAY`, `TELEGRAM_SESSION_NAME`.
- `pyproject.toml` (installable package, optional `media` extra) and a CI smoke test of the installed package.
- 37 new tests (81 in total): settings, command line, package hygiene, module structure, and complete fake-client runs including dry run, resume, deferred replies, formatting fallback and the oversize stop.

## v8.2

- Raises the configured oversize-file threshold to 2000 MiB to match the MTProto upload ceiling described by the project.
- Keeps clean-stop and manual-upload instructions for messages containing files above the configured limit.
- Hardens message-entity validation and limits formatting fallback to recognized entity-bound errors.
- Records single-captioned media state after a successful upload to improve resume behavior.
- Sanitizes archived reaction notes before sending them as text.

## v8.1

- Persists the single-captioned media send state after a successful upload.
- Narrows entity-error detection to avoid misclassifying unrelated exceptions.
- Sanitizes reaction-archive notes and disables parse-mode interpretation for them.

## v8

- Automatically records the source ID of an oversized message when the run stops, allowing a hand-uploaded message to be skipped on resume.

## v7 and earlier

- Adds oversize-media checks, entity bounds hardening, plain-text fallback for rejected formatting entities, FloodWait handling, media-classification updates, hachoir integration, streaming export parsing, and deferred reply reconstruction.
