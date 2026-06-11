"""
cache_contract.py — SHARED CONTRACT: how consumer skills (personal-finance,
retirement) open the monarch-money SQLite cache.

Consumers open the cache READ-ONLY and check `schema_version` on open, refusing
with a clear message rather than failing cryptically when the cache is older
than they need. monarch-money is the only writer — EXCEPT the
`account_tax_buckets.auto_classified=0` user-correction path, which stays
writable by all (it is analysis metadata, not Monarch data; see
open_cache(writable_tax_buckets=True)).

This file is canonical; vendor verbatim into each consumer skill's scripts/.
"""
import os
import sqlite3


class CacheUnavailable(Exception):
    """The cache is missing, unreadable, or too old. Message is user-facing."""


# Minimum cache schema_version each consumer release needs. Bump in a consumer
# only when it starts depending on a newer monarch-money cache table/column.
MIN_SCHEMA_VERSION = 2


def cache_path(data_dir):
    return os.path.join(data_dir, "monarch_cache.db")


def open_cache(data_dir, min_schema=MIN_SCHEMA_VERSION,
               writable_tax_buckets=False):
    """Open the monarch-money cache. Read-only by default (immutable URI);
    raises CacheUnavailable with a user-facing message if absent or too old.

    writable_tax_buckets=True opens read-write ONLY so the caller can update
    account_tax_buckets (the user-correction exception). Even then, callers
    must touch no other table.
    """
    path = cache_path(data_dir)
    if not os.path.exists(path):
        raise CacheUnavailable(
            "No Monarch cache found. Install and run the **monarch-money** "
            "skill first to sync your data (it writes monarch_cache.db).")
    try:
        if writable_tax_buckets:
            conn = sqlite3.connect(path, timeout=5.0)
        else:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise CacheUnavailable(f"Cannot open the Monarch cache: {exc}")

    ver = _schema_version(conn)
    if ver is None:
        conn.close()
        raise CacheUnavailable(
            "The Monarch cache has no schema_version — it predates the skill "
            "split. Update and re-run the monarch-money skill.")
    if ver < min_schema:
        conn.close()
        raise CacheUnavailable(
            f"Monarch cache schema v{ver}, this skill needs >= v{min_schema}. "
            "Update the monarch-money skill and re-sync.")
    return conn


def _schema_version(conn):
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except (sqlite3.Error, ValueError, TypeError):
        return None


def cache_is_present(data_dir):
    return os.path.exists(cache_path(data_dir))
