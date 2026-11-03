"""Rebuild a Telegram Desktop chat export in a channel, resumably, with a Telegram bot.

Quick start::

    from telegram_migration import Settings, run

    settings = Settings.from_env()          # reads TELEGRAM_* variables / .env / Colab Secrets
    run(settings)                           # dry run unless settings.dry_run is False

or from a shell: ``python -m telegram_migration --help``.

Importing this package does not read environment variables or perform file I/O.
Configuration and filesystem access happen when you call :func:`run` / :func:`run_async`.
"""
from ._version import __version__
from .config import ConfigError, Settings

__all__ = ["ConfigError", "Settings", "__version__", "run", "run_async"]


def run(settings=None, **overrides):
    """Run (or resume) a migration. See :func:`telegram_migration.runner.run`."""
    from .runner import run as _run
    return _run(settings, **overrides)


async def run_async(settings=None, **overrides):
    """Coroutine version of :func:`run`. See :func:`telegram_migration.pipeline.run_async`."""
    from .pipeline import run_async as _run_async
    return await _run_async(settings, **overrides)
