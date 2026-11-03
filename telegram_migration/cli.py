"""Command line: ``python -m telegram_migration`` or the ``telegram-migration`` command."""
import argparse
import sys

from ._version import __version__


def confirm_target(channel, pending, n_files, settings):
    print()
    print("═" * 60)
    print(f"  Target:  {getattr(channel, 'title', '?')!r}")
    print(f"  ID:      {getattr(channel, 'id', '?')}")
    uname = getattr(channel, "username", None)
    if uname:
        print(f"  Handle:  @{uname}")
    print(f"  Files:   {n_files} export file(s)")
    print(f"  Pending: ~{pending} messages")
    print("═" * 60)
    if settings.dry_run:
        print("DRY RUN — nothing will actually be sent.\n")
        return True
    if settings.skip_confirmation:
        print("SKIP_CONFIRMATION is on — proceeding.\n")
        return True
    return input("Send to this channel? [y/N]: ").strip().lower() in ("y", "yes")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telegram-migration",
        description="Rebuild a Telegram Desktop chat export in a channel with a Telegram bot. "
                    "Settings come from the TELEGRAM_* environment variables / .env file; "
                    "the options below override them. Dry run is ON unless you pass --send.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--env-file", metavar="PATH", help="path of a .env file to load (default: ./.env if present)")
    parser.add_argument("--export-dir", metavar="PATH", help="folder with messages.html and the media folders")
    parser.add_argument("--channel", metavar="PEER", help="destination: numeric peer id (-100...) or @username")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=None,
                      help="preview only; nothing is sent")
    mode.add_argument("--send", dest="dry_run", action="store_false",
                      help="really send the messages (turns the dry run off)")
    parser.add_argument("--yes", dest="skip_confirmation", action="store_true", default=None,
                        help="do not ask for confirmation before sending")
    parser.add_argument("--max-upload-mb", type=float, metavar="N",
                        help="stop at any message with a file larger than this (0 disables)")
    parser.add_argument("--batch-limit", type=int, metavar="N", help="stop after this many messages")
    parser.add_argument("--delay", type=float, dest="send_delay_seconds", metavar="SECONDS",
                        help="seconds to wait between messages")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    overrides = {}
    if args.export_dir is not None:
        overrides["base_dir"] = args.export_dir
    if args.channel is not None:
        try:
            overrides["channel_peer"] = int(args.channel)
        except ValueError:
            overrides["channel_peer"] = args.channel
    for name in ("dry_run", "skip_confirmation", "batch_limit", "send_delay_seconds"):
        if getattr(args, name) is not None:
            overrides[name] = getattr(args, name)
    if args.max_upload_mb is not None:
        overrides["max_upload_mb"] = args.max_upload_mb or None

    from .config import ConfigError, Settings            # imported late so --help/--version stay light
    try:
        settings = Settings.from_env(env_file=args.env_file, **overrides).validate()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    from .runner import run
    try:
        run(settings)
    except KeyboardInterrupt:
        print("\nInterrupted. Progress made so far is saved; run again to resume.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":      # python -m telegram_migration.cli
    raise SystemExit(main())
