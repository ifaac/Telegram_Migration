"""Settings for a migration run.

Build a :class:`Settings` yourself, or call :meth:`Settings.from_env` to read the
``TELEGRAM_*`` environment variables (a local ``.env`` file and Google Colab Secrets are
supported). The package modules read the *active* settings through ``config.settings``
at call time, so :func:`use_settings` / :func:`set_settings` change them everywhere.

Nothing is read from the environment at import time.
"""
from __future__ import annotations

import dataclasses
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple, Union

PLACEHOLDER_API_ID = 12345678          # the value shipped in .env.example
_TRUE = {"1", "true", "yes", "y", "on"}

# Progress files kept inside the export folder: attribute name -> file name.
_DERIVED_FILES = {
    "idmap_file": "migration_id_map.jsonl",
    "failed_file": "failed_messages.txt",
    "partial_file": "migration_partial_sends.jsonl",
    "pending_replies_file": "migration_pending_replies.json",
    "reaction_note_map_file": "migration_reaction_notes.jsonl",
    "reaction_archive_file": "migration_reactions_archive.jsonl",
    "reaction_source_file": "migration_reactions_source.jsonl",
    "reply_audit_file": "migration_reply_audit.jsonl",
    "format_debug_file": "migration_formatting_dropped.jsonl",
    "manual_uploads_file": "migration_manual_uploads.txt",
}


class ConfigError(ValueError):
    """The settings are incomplete or still contain example values."""


def _truthy(value: Optional[str], default: bool) -> bool:
    if value is None or not str(value).strip():
        return default
    return str(value).strip().casefold() in _TRUE


def load_secret(name: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Read a value from the environment, then (in Google Colab) from Colab Secrets."""
    source = os.environ if env is None else env
    value = (source.get(name, "") or "").strip()
    if value or env is not None:
        return value
    try:
        from google.colab import userdata  # type: ignore
        return (userdata.get(name) or "").strip()
    except Exception:
        return ""


def _load_dotenv(env_file: Optional[str]) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(env_file) if env_file else load_dotenv()


@dataclass
class Settings:
    """Everything that can be configured. Defaults are the safe ones (dry run on)."""

    # --- Telegram credentials -------------------------------------------------------------
    api_id: Optional[int] = None
    api_hash: str = field(default="", repr=False)
    bot_token: str = field(default="", repr=False)
    channel_peer: Union[int, str, None] = None     # numeric peer id (-100...) or "@username"
    session_name: str = "colab_bot_restorer_session"

    # --- where things live ----------------------------------------------------------------
    base_dir: str = "./telegram_export"            # folder with messages.html, media folders, ...
    # Progress files; when left as None they are placed inside base_dir.
    idmap_file: Optional[str] = None
    failed_file: Optional[str] = None
    partial_file: Optional[str] = None
    pending_replies_file: Optional[str] = None
    reaction_note_map_file: Optional[str] = None
    reaction_archive_file: Optional[str] = None
    reaction_source_file: Optional[str] = None
    reply_audit_file: Optional[str] = None
    format_debug_file: Optional[str] = None
    manual_uploads_file: Optional[str] = None

    # --- behaviour --------------------------------------------------------------------------
    dry_run: bool = True                           # preview only; nothing is sent
    skip_confirmation: bool = False                # do not ask before sending
    # Largest file the run will try to upload; a message with a bigger file stops the run
    # (nothing from it is sent) so it can be uploaded by hand. None or 0 disables the check.
    # 2000 MiB is believed to be Telegram's MTProto ceiling; verify it with your bot.
    max_upload_mb: Optional[float] = 2000
    # On an oversize stop, record that message in manual_uploads_file so the next run skips it.
    # UPLOAD THE FILE(S) BEFORE RERUNNING, or that message is left out of the channel.
    auto_mark_manual_on_stop: bool = True
    batch_limit: int = 1000                        # stop after this many messages (sent + failed)
    max_retries: int = 5
    max_flood_waits: int = 10
    send_delay_seconds: float = 2.5
    preserve_sender_names: bool = True
    preserve_reactions_as_annotations: bool = True
    json_reaction_file_candidates: Tuple[str, ...] = ("result.json", "messages.json")
    max_json_fallback_mb: int = 512
    verify_replies: bool = True
    caption_limit: int = 1024                      # 2048 for Telegram Premium accounts
    # Try a native forward first when the export links the original message; a native
    # forward cannot carry our timestamp/sender line, so forward_annotate sends it as a reply.
    native_forward: bool = True
    forward_annotate: bool = True

    def __post_init__(self) -> None:
        self.base_dir = os.path.abspath(os.path.expanduser(str(self.base_dir)))
        for attr, filename in _DERIVED_FILES.items():
            if getattr(self, attr) is None:
                setattr(self, attr, os.path.join(self.base_dir, filename))

    # ------------------------------------------------------------------------------------
    def replace(self, **changes) -> "Settings":
        """Copy with some fields changed. Changing base_dir moves the progress files too."""
        if "base_dir" in changes:
            for attr in _DERIVED_FILES:
                changes.setdefault(attr, None)
        return dataclasses.replace(self, **changes)

    def problems(self) -> list:
        """Human-readable list of what is missing or still an example value."""
        found = []
        if self.api_id is None:
            found.append("TELEGRAM_API_ID is not set")
        elif self.api_id == PLACEHOLDER_API_ID:
            found.append("TELEGRAM_API_ID is still the example value; use your own from my.telegram.org")
        for env_name, value in (("TELEGRAM_API_HASH", self.api_hash), ("TELEGRAM_BOT_TOKEN", self.bot_token)):
            if not value:
                found.append(f"{env_name} is not set")
            elif value.startswith("replace_with"):
                found.append(f"{env_name} is still the example value")
        if self.channel_peer in (None, ""):
            found.append("TELEGRAM_CHANNEL_PEER is not set (numeric peer id or @username)")
        return found

    def validate(self) -> "Settings":
        """Raise ConfigError if credentials or the destination are missing."""
        found = self.problems()
        if found:
            raise ConfigError(
                "; ".join(found) + ". Set them in .env/the environment or as Colab Secrets. "
                "Never commit credentials to Git."
            )
        return self

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, *, env_file: Optional[str] = None,
                 use_dotenv: bool = True, **overrides) -> "Settings":
        """Read the TELEGRAM_* variables. Explicit keyword arguments win over the environment.

        With env=None the process environment is used, a local .env file is loaded first
        (it never overrides variables that are already set) and Colab Secrets are consulted.
        Pass a mapping as ``env`` to read from it only (useful in tests).
        """
        if env is None and use_dotenv:
            _load_dotenv(env_file)
        get = lambda name: load_secret(name, env)          # noqa: E731

        values: dict = {}
        raw_id = get("TELEGRAM_API_ID")
        if raw_id:
            try:
                values["api_id"] = int(raw_id)
            except ValueError:
                raise ConfigError(f"TELEGRAM_API_ID must be a number, got {raw_id!r}") from None
        values["api_hash"] = get("TELEGRAM_API_HASH")
        values["bot_token"] = get("TELEGRAM_BOT_TOKEN")
        raw_peer = get("TELEGRAM_CHANNEL_PEER")
        if raw_peer:
            try:
                values["channel_peer"] = int(raw_peer)
            except ValueError:
                values["channel_peer"] = raw_peer
        source = os.environ if env is None else env
        if source.get("TELEGRAM_EXPORT_DIR"):
            values["base_dir"] = source["TELEGRAM_EXPORT_DIR"]
        if source.get("TELEGRAM_SESSION_NAME"):
            values["session_name"] = source["TELEGRAM_SESSION_NAME"]
        values["dry_run"] = _truthy(source.get("TELEGRAM_DRY_RUN"), True)
        values["skip_confirmation"] = _truthy(source.get("TELEGRAM_SKIP_CONFIRMATION"), False)

        raw = (source.get("TELEGRAM_MAX_UPLOAD_MB") or "").strip().casefold()
        if raw:
            values["max_upload_mb"] = None if raw in {"0", "none", "off", "false"} else _number(raw, "TELEGRAM_MAX_UPLOAD_MB", float)
        raw = (source.get("TELEGRAM_BATCH_LIMIT") or "").strip()
        if raw:
            values["batch_limit"] = _number(raw, "TELEGRAM_BATCH_LIMIT", int)
        raw = (source.get("TELEGRAM_SEND_DELAY") or "").strip()
        if raw:
            values["send_delay_seconds"] = _number(raw, "TELEGRAM_SEND_DELAY", float)

        values.update(overrides)
        return cls(**values)


def _number(raw: str, name: str, kind):
    try:
        return kind(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None


# The settings the package modules read. Defaults only: nothing is read from the environment
# until Settings.from_env() (or run()) is called.
settings: Settings = Settings()


def set_settings(new: Settings) -> Settings:
    """Make `new` the active settings for the whole package."""
    global settings
    settings = new
    return new


@contextmanager
def use_settings(new: Settings):
    """Temporarily activate `new` (restored afterwards)."""
    global settings
    previous = settings
    settings = new
    try:
        yield new
    finally:
        settings = previous


def resolve_settings(settings_arg: Optional[Settings] = None, **overrides) -> Settings:
    """`settings_arg` (or Settings.from_env()) with keyword overrides applied."""
    if settings_arg is None:
        return Settings.from_env(**overrides)
    return settings_arg.replace(**overrides) if overrides else settings_arg
