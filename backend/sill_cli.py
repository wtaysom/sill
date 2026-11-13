"""
sill — top-level CLI for the Sill memory system.

Subcommands wired so far:

  sill seed import <path>   Import a JSONL methodology seed into the db
                            (Task 15)
  sill db psql              Drop into a psql shell against the db container
                            (wired in a later task)
  sill connect <client>     Register this instance with a local MCP client.
  sill verify               Run the standard smoke-check suite
                            (wired in a later task)
  sill notice ...           Store one deliberate memory (the mint path);
                            delegates to sill.py's own parser so this
                            subcommand's argv shape lives in exactly one
                            place. Run 'sill notice --help' for its flags.
  sill identity show|init|set
                            Read or write this instance's identity card
                            (name, charter, engine, scope, harnesses) —
                            see backend/scripts/identity_card.py and
                            docs/identity.md. Run 'sill identity --help'.
  sill backfill plan|run   Consent-scoped scan of harness transcripts into
                            a durable, undoable archive. 'plan' reports
                            what would be read and writes nothing; 'run'
                            requires --confirm. See
                            backend/scripts/backfill_scan.py and
                            docs/onboarding/02-backfill.md. Run
                            'sill backfill --help'.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


def _cmd_seed_import(args: argparse.Namespace, _extra: list[str]) -> int:
    # Lazy import so `sill --help` works even if psycopg2 isn't installed yet.
    from scripts.seed_import import main as _seed_import_main

    if args.env_file is None:
        summary = _seed_import_main(args.path)
    else:
        from instance_config import InstanceConfigError, instance_dsn, load_instance_env

        try:
            dsn = instance_dsn(load_instance_env(args.env_file))
        except InstanceConfigError as exc:
            print(f"sill seed import: {exc}", file=sys.stderr)
            return 1
        summary = _seed_import_main(args.path, dsn=dsn)
    return 0 if summary.errors == 0 else 1


def _cmd_db_psql(_args: argparse.Namespace, _extra: list[str]) -> int:
    raise NotImplementedError(
        "db psql — wired in a later task (drops into psql against $SILL_DB_CONTAINER)"
    )


def _cmd_connect(args: argparse.Namespace, _extra: list[str]) -> int:
    from client_config import ClientConfigError, client_config_path, register_client

    command = args.mcp_command
    if command is None:
        # pipx may expose sill as a symlink. Prefer the entry point's own
        # bin directory and fall back to the interpreter's environment.
        launcher = Path(sys.argv[0]).resolve().with_name("sill-mcp")
        command = launcher if launcher.is_file() else Path(sys.executable).with_name("sill-mcp")
    try:
        status = register_client(
            args.client, args.env_file, command,
            dry_run=args.dry_run, adopt_legacy=args.adopt_legacy,
        )
    except ClientConfigError as exc:
        print(f"sill connect: {exc}", file=sys.stderr)
        return 1
    print(f"sill connect: {status} in {client_config_path(args.client)}")
    return 0


def _cmd_verify(_args: argparse.Namespace, _extra: list[str]) -> int:
    raise NotImplementedError(
        "verify — wired in a later task (mirrors verify.sh smoke checks)"
    )


def _cmd_notice(args: argparse.Namespace, extra: list[str]) -> int:
    """Delegate to the mint module's own parser so 'sill notice ...' and
    'python sill.py notice ...' accept identical argv."""
    import sill

    return sill.main(["notice", *extra])


def _cmd_identity(args: argparse.Namespace, extra: list[str]) -> int:
    """Delegate to identity_card's own parser so 'sill identity ...' and
    'python -m scripts.identity_card ...' accept identical argv."""
    from scripts.identity_card import main as _identity_main

    return _identity_main(extra)


def _cmd_backfill(args: argparse.Namespace, extra: list[str]) -> int:
    """Delegate to backfill_scan's own parser so 'sill backfill ...' and
    'python -m scripts.backfill_scan ...' accept identical argv."""
    from scripts.backfill_scan import main as _backfill_main

    return _backfill_main(extra)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sill",
        description="Sill memory system CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    # seed
    seed = sub.add_parser("seed", help="Manage seed memory packs")
    seed_sub = seed.add_subparsers(dest="subcommand", required=True, metavar="<subcommand>")
    seed_import = seed_sub.add_parser(
        "import",
        help="Import a JSONL methodology seed into the running db",
    )
    seed_import.add_argument("path", help="Path to a JSONL seed file")
    seed_import.add_argument("--env-file", type=Path, help="Absolute path to the instance dotenv file")
    seed_import.set_defaults(func=_cmd_seed_import)

    # db
    db = sub.add_parser("db", help="Database utilities")
    db_sub = db.add_subparsers(dest="subcommand", required=True, metavar="<subcommand>")
    db_psql = db_sub.add_parser("psql", help="Open a psql shell against the db container")
    db_psql.set_defaults(func=_cmd_db_psql)

    connect = sub.add_parser("connect", help="Register this instance with a local MCP client")
    connect.add_argument("client", choices=("claude-code", "codex", "claude-desktop"))
    connect.add_argument("--env-file", type=Path, required=True, help="Absolute path to the instance dotenv file")
    connect.add_argument("--mcp-command", type=Path, help="Absolute path to the sill-mcp executable")
    connect.add_argument("--dry-run", action="store_true", help="Validate registration without changing files")
    connect.add_argument("--adopt-legacy", action="store_true", help="Replace an ambiguous bare sill-mcp entry")
    connect.set_defaults(func=_cmd_connect)

    # verify
    verify = sub.add_parser("verify", help="Run the smoke-check suite")
    verify.set_defaults(func=_cmd_verify)

    # notice — the mint path. This subparser deliberately declares none of
    # notice's own arguments (content, --speaker, --force, --receipt-to, ...)
    # and disables its own -h/--help, so every flag INCLUDING --help falls
    # through parse_known_args() as "extra" and reaches sill.py's own parser
    # untouched. That keeps the argv shape defined in exactly one place
    # (sill.build_parser) and makes 'sill notice --help' show sill.py's real
    # flags rather than this shell's empty usage line.
    notice = sub.add_parser(
        "notice",
        help="Store one deliberate memory (the mint path). See 'sill notice --help'.",
        add_help=False,
    )
    notice.set_defaults(func=_cmd_notice)

    # identity — the identity card (show/init/set). Same passthrough shape
    # as notice above: this subparser declares none of identity_card's own
    # subcommands or flags and disables its own -h/--help, so 'sill identity
    # --help' shows identity_card's real usage rather than this shell's
    # empty one, and the argv shape lives in exactly one place
    # (identity_card.build_parser).
    identity = sub.add_parser(
        "identity",
        help="Read or write this instance's identity card. See 'sill identity --help'.",
        add_help=False,
    )
    identity.set_defaults(func=_cmd_identity)

    # backfill — consent-scoped episodic backfill (plan/run). Same
    # passthrough shape as notice/identity above: this subparser declares
    # none of backfill_scan's own subcommands or flags and disables its own
    # -h/--help, so 'sill backfill --help' shows backfill_scan's real usage
    # and the argv shape lives in exactly one place (backfill_scan.build_parser).
    backfill = sub.add_parser(
        "backfill",
        help="Consent-scoped scan of harness transcripts. See 'sill backfill --help'.",
        add_help=False,
    )
    backfill.set_defaults(func=_cmd_backfill)

    return parser


# Subcommands whose whole design is "forward argv to a nested module's own
# parser untouched" (see their build_parser() entries above) — for these
# alone, parse_known_args()'s leftover `extra` is not a typo, it's the
# point. Every other subcommand declares its own arguments the normal way,
# so leftover `extra` there really is unrecognized input and must be
# rejected the way parse_args() would reject it directly.
PASSTHROUGH_COMMANDS = frozenset({"notice", "identity", "backfill"})


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    if extra and getattr(args, "command", None) not in PASSTHROUGH_COMMANDS:
        # Mirrors argparse's own parse_args()/error() wording and exit code
        # (2) — parse_known_args() alone can't reject this per-subparser,
        # so main() does it explicitly for every subcommand that isn't a
        # documented passthrough.
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    try:
        return int(func(args, extra) or 0)
    except NotImplementedError as exc:
        print(f"sill: {exc}", file=sys.stderr)
        return 64  # EX_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
