"""
cli.py — CLI scaffold with the subcommands every skill ships.

MOSTLY-SHARED SKELETON FILE: the built-ins (version / status / clear-cache)
and the argparse plumbing come from the skeleton; add your skill's
subcommands in the marked section. Keep the built-ins unmodified so skeleton
updates merge cleanly.

Conventions baked in:
  * compact Markdown to stdout; never raw data dumps (Claude queries the
    SQLite cache directly for analysis)
  * fatal errors -> {"error": ..., "message": ...} JSON on stdout, exit 1
  * --mock loads tests/fixtures/<subcommand>.json instead of the network
  * --refresh bypasses TTLs
  * NO_AUTH commands never touch credentials (purely local)
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from skill_cache import SkillCache                      # noqa: E402
from skill_common import (                              # noqa: E402
    SkillError, fail, force_utf8_stdout, age_phrase,
)
import skill_schema                                     # noqa: E402

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(SKILL_DIR, "data", "cache.db")
DEFAULT_FIXTURES_DIR = os.path.join(
    os.path.dirname(SKILL_DIR), "tests", "fixtures")

# Commands that never need credentials (extend in the per-skill section).
NO_AUTH = {"version", "status", "clear-cache"}


def load_fixture(fixtures_dir, name):
    with open(os.path.join(fixtures_dir, f"{name}.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# Built-in renderers (shared)
# --------------------------------------------------------------------------- #
def render_version():
    print(f"skill version {skill_schema.SKILL_VERSION}")


def render_status(cache):
    st = cache.status()
    size_mb = st["db_size_bytes"] / (1024 * 1024)
    print("## Cache Status\n")
    print(f"Skill version: {skill_schema.SKILL_VERSION}")
    print(f"Database: {st['db_path']} ({size_mb:.1f} MB)")
    for table in skill_schema.SCHEMA["tables"]:
        print(f"{table}: {st[f'{table}_rows']} rows, last synced "
              f"{age_phrase(st[f'{table}_last_sync'])}")
    print(f"Total API calls (lifetime): {st['api_call_count']}")


def render_clear(cache, force):
    counts = cache.clear(force=force)
    print("## Cache Cleared\n")
    for table in skill_schema.SCHEMA["tables"]:
        print(f"✓ {table}: {counts[table]} rows removed")
    if counts["protected_rows_kept"]:
        print(f"  ({counts['protected_rows_kept']} protected row(s) kept — "
              f"use --force to remove)")
    elif force:
        print("  (protected rows wiped — --force)")
    print("\nNext sync re-pulls everything fresh.")


# --------------------------------------------------------------------------- #
# >>> SKILL-SPECIFIC SECTION — add your subcommands below <<<
#
# Pattern per subcommand:
#   1. skip auth if cmd in NO_AUTH or args.mock
#   2. skip the API when cache.any_fresh(table) and not args.refresh
#   3. with --mock, read tests/fixtures/<cmd>.json
#   4. normalise rows -> write to cache -> cache.increment_counter()
#   5. print a compact Markdown summary
# --------------------------------------------------------------------------- #
def dispatch_skill_command(args, cache):
    """Return an exit code for skill-specific commands, or None if the
    command isn't one of yours (lets the built-ins run)."""
    return None


def add_skill_subparsers(sub, common):
    """Register skill-specific subcommands, e.g.:
    p = sub.add_parser("items", parents=[common]); p.add_argument(...)"""


# --------------------------------------------------------------------------- #
# Plumbing (shared)
# --------------------------------------------------------------------------- #
def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--refresh", action="store_true",
                        help="Bypass cache TTL.")
    common.add_argument("--mock", action="store_true",
                        help="Load fixtures, no network.")
    common.add_argument("--db", default=DEFAULT_DB_PATH)
    common.add_argument("--fixtures-dir", default=DEFAULT_FIXTURES_DIR)

    p = argparse.ArgumentParser(description="Skill data CLI.",
                                parents=[common])
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("version", parents=[common])
    sub.add_parser("status", parents=[common])
    cc = sub.add_parser("clear-cache", parents=[common])
    cc.add_argument("--force", action="store_true",
                    help="Also remove protected rows (data the API cannot "
                         "regenerate, e.g. user corrections).")
    add_skill_subparsers(sub, common)
    return p


def main(argv=None):
    force_utf8_stdout()
    args = build_parser().parse_args(argv)
    cache = None
    try:
        cache = SkillCache(args.db, skill_schema.SCHEMA)
        if args.command == "version":
            render_version()
        elif args.command == "status":
            render_status(cache)
        elif args.command == "clear-cache":
            render_clear(cache, args.force)
        else:
            code = dispatch_skill_command(args, cache)
            if code is None:
                raise SystemExit(f"unknown command: {args.command}")
            return code
        return 0
    except SkillError as exc:
        return fail(exc)
    finally:
        if cache is not None:
            cache.close()


if __name__ == "__main__":
    sys.exit(main())
