"""Synchronous entry point for scripts and notebooks."""
import asyncio
import subprocess
import sys

from . import config, pipeline


def run(settings=None, **overrides):
    """Run (or resume) a migration from ordinary synchronous code.

    ``run()`` reads the settings from the environment; ``run(settings)`` uses a Settings
    object; keyword arguments override single settings, e.g. ``run(dry_run=False)``.

    Works from a plain script and from Google Colab / Jupyter: inside an already running
    event loop it patches that loop with ``nest_asyncio`` and runs on the notebook's main
    thread, so ``input()`` and ``drive.mount()`` keep working.
    """
    resolved = config.resolve_settings(settings, **overrides).validate()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(pipeline.run_async(resolved))

    try:
        import nest_asyncio
    except ImportError:
        print("Installing the small nest_asyncio helper for notebook execution...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "nest_asyncio"])
        import nest_asyncio

    nest_asyncio.apply(loop)
    return loop.run_until_complete(pipeline.run_async(resolved))
