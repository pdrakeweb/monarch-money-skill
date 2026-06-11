"""
manage_exclusions.py — manage the non-recurring-expense exclusions layer.

Large one-time expenses (orthodontia, surgery, an annual insurance lump, a big
home repair) skew monthly cashflow, savings rate, and category trends. This CLI
lets the user mark them as excluded so they drop out of ongoing transaction
analysis by default (see fetch.py's `--include-excluded` to see the raw view).

    python scripts/manage_exclusions.py list
    python scripts/manage_exclusions.py add --type category_period \
        --value "Dental & Medical" --start 2024-03 --end 2024-03 --reason orthodontia
    python scripts/manage_exclusions.py remove --id 3
    python scripts/manage_exclusions.py suggest
    python scripts/manage_exclusions.py clear-auto

Reads the SQLite cache only — never authenticates, so it works offline and
never surfaces raw financial data in conversation context. On fatal error it
prints {"error","message"} JSON and exits 1, like fetch.py.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from monarch_client import (  # noqa: E402
    MonarchCache, MonarchSkillError, DEFAULT_DB_PATH,
)
import analysis  # noqa: E402

_TYPES = ("transaction", "category", "merchant", "tag", "category_period")
_SCOPES = ("budget", "cashflow", "insights", "all")


def _fmt_window(start, end):
    if not start and not end:
        return "all time"
    return f"{start or '…'} → {end or '…'}"


def cmd_list(cache, args):
    rows = cache.list_exclusions(type_filter=args.type)
    if not rows:
        print("No exclusions configured. Run `suggest` to find candidates.")
        return 0
    print(f"## Exclusions ({len(rows)})\n")
    print("| ID | Type | Value | Window | Scope | Source | Reason |")
    print("|----|------|-------|--------|-------|--------|--------|")
    for r in rows:
        src = "auto" if r["auto_suggested"] else "user"
        print(f"| {r['id']} | {r['type']} | {r['value']} | "
              f"{_fmt_window(r['start_date'], r['end_date'])} | "
              f"{r['exclude_from']} | {src} | {r['reason'] or ''} |")
    return 0


def cmd_add(cache, args):
    if not args.value:
        raise MonarchSkillError("--value is required for add.")
    before = cache._conn.execute(
        "SELECT COUNT(*) FROM exclusions").fetchone()[0]
    ex_id = cache.add_exclusion(
        type=args.type, value=args.value, start_date=args.start,
        end_date=args.end, reason=args.reason, exclude_from=args.scope)
    after = cache._conn.execute(
        "SELECT COUNT(*) FROM exclusions").fetchone()[0]
    if after > before:
        print(f"Exclusion added (ID {ex_id}): {args.type} '{args.value}' "
              f"excluded from {args.scope}.")
    else:
        print(f"Exclusion already exists (ID {ex_id}).")
    return 0


def cmd_remove(cache, args):
    if cache.remove_exclusion(args.id):
        print(f"Removed exclusion {args.id}.")
    else:
        print(f"Exclusion {args.id} not found.")
    return 0


def cmd_clear_auto(cache, args):
    n = cache.clear_auto_exclusions()
    print(f"Removed {n} auto-suggested exclusion(s).")
    return 0


def cmd_suggest(cache, args):
    rows = cache._conn.execute(
        "SELECT id, date, amount, merchant, category, notes FROM transactions"
    ).fetchall()
    if not rows:
        print("Run `python scripts/fetch.py transactions --days 365` to "
              "populate the cache first.")
        return 0
    cols = ("id", "date", "amount", "merchant", "category", "notes")
    txns = [dict(zip(cols, r)) for r in rows]
    suggestions = analysis.suggest_exclusions(txns)
    if not suggestions:
        print("## Suggested Exclusions\n\nNo non-recurring candidates found "
              "in the cached transactions.")
        return 0
    print("## Suggested Exclusions\n")
    last_conf = None
    for s in suggestions:
        if s["confidence"] != last_conf:
            print(f"\nConfidence: {s['confidence']}")
            last_conf = s["confidence"]
        window = _fmt_window(s["start_date"], s["end_date"])
        print(f"  {s['type']}: {s['value']} | Period: {window} | {s['reason']}")
        print(f"  → {s['suggested_command']}")
    print("\nReview each candidate; these are NOT added automatically. Run a "
          "suggested command to add one (they persist across syncs).")
    return 0


def build_parser():
    # --db lives on a shared parent so it may follow the subcommand
    # (e.g. `manage_exclusions.py list --db PATH`), matching fetch.py.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=DEFAULT_DB_PATH)

    p = argparse.ArgumentParser(
        description="Manage non-recurring-expense exclusions (offline; reads "
                    "the SQLite cache only).", parents=[common])
    sub = p.add_subparsers(dest="command", required=True)

    lst = sub.add_parser("list", parents=[common],
                         help="Show all active exclusions.")
    lst.add_argument("--type", choices=_TYPES, help="Filter by exclusion type.")

    add = sub.add_parser("add", parents=[common], help="Add an exclusion.")
    add.add_argument("--type", required=True, choices=_TYPES)
    add.add_argument("--value", required=True,
                     help="tx ID / category / merchant / tag name.")
    add.add_argument("--start", help="ISO date or YYYY-MM (optional).")
    add.add_argument("--end", help="ISO date or YYYY-MM (optional).")
    add.add_argument("--reason", help="Human note.")
    add.add_argument("--scope", choices=_SCOPES, default="all")

    rm = sub.add_parser("remove", parents=[common],
                        help="Remove an exclusion by ID.")
    rm.add_argument("--id", type=int, required=True)

    sub.add_parser("suggest", parents=[common],
                   help="Scan the cache for candidates (prints ready-to-run "
                        "add commands; adds nothing).")
    sub.add_parser("clear-auto", parents=[common],
                   help="Remove all auto-suggested exclusions.")
    return p


_HANDLERS = {
    "list": cmd_list, "add": cmd_add, "remove": cmd_remove,
    "suggest": cmd_suggest, "clear-auto": cmd_clear_auto,
}


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    cache = None
    try:
        cache = MonarchCache(args.db)
        return _HANDLERS[args.command](cache, args)
    except MonarchSkillError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
        return 1
    finally:
        if cache is not None:
            cache.close()


if __name__ == "__main__":
    sys.exit(main())
