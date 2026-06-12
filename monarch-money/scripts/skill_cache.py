"""
skill_cache — generic, schema-driven SQLite cache for Claude skills.

SHARED SKELETON FILE — improve in claude-skill-skeleton and pull into skills;
avoid skill-specific edits here (put those in skill_schema.py).

The skill describes its storage in skill_schema.py and this module provides
the machinery: TTL freshness, lifetime counters, status, prune, writability
fallback, and a clear() whose semantics are driven entirely by the schema:

    SCHEMA = {
        "ddl": "CREATE TABLE IF NOT EXISTS ...;",   # all skill tables
        "tables": {
            "items":       {"ttl": 4 * 3600},
            # Rows matching protect_where survive a normal clear and are
            # only removed by clear(force=True)  (e.g. user-entered data
            # the API cannot regenerate).
            "annotations": {"ttl": None, "protect_where": "user_set = 1"},
        },
        # meta keys removed on clear (sync markers); counters always survive.
        "clear_meta_keys": ["last_full_sync", "last_incremental_sync"],
    }

Every skill gets a `meta` table automatically (key/value: schema_version,
api_call_count, sync markers, anything via get/set_meta).
"""
import os
import sqlite3
import tempfile
import time

_META_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class SkillCache:
    """SQLite-backed cache configured by a skill's SCHEMA dict."""

    def __init__(self, db_path, schema):
        self.schema = schema
        self.db_path = db_path
        if db_path != ":memory:":
            self.db_path = self._ensure_writable(db_path)
        self._conn = sqlite3.connect(self.db_path, timeout=5.0)
        self._conn.executescript(_META_DDL + schema["ddl"])
        self.set_meta("schema_version", str(schema.get("version", 1)))
        self._conn.commit()

    # ---- writability fallback ------------------------------------------ #
    def _fallback_path(self):
        stamp = time.strftime("%Y%m%d")
        base = os.path.splitext(os.path.basename(self.db_path))[0] or "skill_cache"
        return os.path.join(tempfile.gettempdir(), f"{base}_{stamp}.db")

    def _ensure_writable(self, db_path):
        parent = os.path.dirname(os.path.abspath(db_path))
        try:
            os.makedirs(parent, exist_ok=True)
            return db_path
        except PermissionError:
            return self._fallback_path()

    # ---- meta ------------------------------------------------------------ #
    def set_meta(self, key, value):
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self._conn.commit()

    def get_meta(self, key, default=None):
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    def increment_counter(self, key="api_call_count", n=1):
        current = int(self.get_meta(key) or 0)
        self.set_meta(key, current + n)

    # ---- freshness -------------------------------------------------------- #
    def _table_ttl(self, table):
        return (self.schema["tables"].get(table) or {}).get("ttl")

    def any_fresh(self, table, ttl=None):
        """True iff the table has at least one row newer than its TTL
        (or the explicit ttl argument). Tables need a fetched_at column."""
        ttl = ttl if ttl is not None else self._table_ttl(table)
        if ttl is None:
            return False
        row = self._conn.execute(
            f"SELECT MAX(fetched_at) FROM {table}"
        ).fetchone()
        if not row or row[0] is None:
            return False
        return (time.time() - row[0]) < ttl

    def is_fresh(self, table, key_col, key_val, ttl=None):
        ttl = ttl if ttl is not None else self._table_ttl(table)
        if ttl is None:
            return False
        row = self._conn.execute(
            f"SELECT MAX(fetched_at) FROM {table} WHERE {key_col} = ?",
            (key_val,),
        ).fetchone()
        if not row or row[0] is None:
            return False
        return (time.time() - row[0]) < ttl

    # ---- status ------------------------------------------------------------ #
    def status(self):
        out = {}
        for table in self.schema["tables"]:
            out[f"{table}_rows"] = self._conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            try:
                out[f"{table}_last_sync"] = self._conn.execute(
                    f"SELECT MAX(fetched_at) FROM {table}"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                out[f"{table}_last_sync"] = None  # table has no fetched_at
        out["api_call_count"] = int(self.get_meta("api_call_count") or 0)
        out["schema_version"] = self.get_meta("schema_version")
        if self.db_path != ":memory:" and os.path.exists(self.db_path):
            out["db_size_bytes"] = os.path.getsize(self.db_path)
        else:
            out["db_size_bytes"] = 0
        out["db_path"] = self.db_path
        return out

    # ---- clear ------------------------------------------------------------ #
    def clear(self, force=False):
        """Empty the cache so the next sync re-pulls everything.

        Schema-driven: rows matching a table's `protect_where` survive a
        normal clear (they hold data the upstream API cannot regenerate —
        e.g. user-entered corrections) and are only removed with force=True.
        Lifetime counters survive; meta keys in clear_meta_keys are reset.
        Returns {table: rows_removed, ..., "protected_rows_kept": n}.
        """
        counts = {}
        kept_total = 0
        for table, spec in self.schema["tables"].items():
            protect = (spec or {}).get("protect_where")
            if protect and not force:
                counts[table] = self._conn.execute(
                    f"DELETE FROM {table} WHERE NOT ({protect})"
                ).rowcount
                kept_total += self._conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            else:
                counts[table] = self._conn.execute(
                    f"DELETE FROM {table}"
                ).rowcount
        for key in self.schema.get("clear_meta_keys", []):
            self._conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        self._conn.commit()
        try:
            self._conn.execute("VACUUM")
        except sqlite3.Error:
            pass
        counts["protected_rows_kept"] = kept_total
        return counts

    # ---- config export / import ------------------------------------------ #
    # Non-reproducible config = the user-entered data the upstream API can NOT
    # regenerate. It lives in two places: tables listed in SCHEMA["config_tables"]
    # (kept entirely out of the TTL cache, e.g. exclusions / goals), and the
    # protect_where rows of regular cache tables (e.g. user tax-bucket
    # corrections). export_config() captures BOTH into a portable dict so a
    # session can hand the user a durable artifact; import_config() restores it
    # — the durability backstop for the cloud "every new chat is a fresh
    # container" reset (and for homedir wipes / new machines).
    def _table_columns(self, table):
        return [r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")]

    def export_config(self):
        """Return {config_tables: {t: {columns, rows}}, protected_rows:
        {t: {columns, where, rows}}} — everything non-reproducible this cache
        holds. Reproducible cache rows are deliberately excluded."""
        out = {"config_tables": {}, "protected_rows": {}}
        for table in self.schema.get("config_tables", []):
            try:
                cols = self._table_columns(table)
                rows = self._conn.execute(f"SELECT * FROM {table}").fetchall()
            except sqlite3.OperationalError:
                continue
            out["config_tables"][table] = {
                "columns": cols, "rows": [list(r) for r in rows]}
        for table, spec in self.schema["tables"].items():
            protect = (spec or {}).get("protect_where")
            if not protect:
                continue
            try:
                cols = self._table_columns(table)
                rows = self._conn.execute(
                    f"SELECT * FROM {table} WHERE {protect}").fetchall()
            except sqlite3.OperationalError:
                continue
            if rows:
                out["protected_rows"][table] = {
                    "columns": cols, "where": protect,
                    "rows": [list(r) for r in rows]}
        return out

    def import_config(self, bundle):
        """Restore an export_config() bundle (INSERT OR REPLACE — idempotent;
        existing rows with the same key/id or unique constraint are overwritten
        by the imported copy). Returns {table: rows_restored}."""
        counts = {}
        for section in ("config_tables", "protected_rows"):
            for table, blob in (bundle.get(section) or {}).items():
                cols = blob.get("columns") or []
                if not cols:
                    continue
                collist = ",".join(cols)
                placeholders = ",".join("?" * len(cols))
                n = 0
                for row in blob.get("rows", []):
                    self._conn.execute(
                        f"INSERT OR REPLACE INTO {table} ({collist}) "
                        f"VALUES ({placeholders})", list(row))
                    n += 1
                counts[table] = counts.get(table, 0) + n
        self._conn.commit()
        return counts

    # ---- prune ------------------------------------------------------------ #
    def prune_stale(self):
        """Delete rows older than 2x their table TTL (safety margin).
        Tables without a ttl are never pruned. Protected rows survive."""
        now = int(time.time())
        deleted = 0
        for table, spec in self.schema["tables"].items():
            ttl = (spec or {}).get("ttl")
            if not ttl:
                continue
            cutoff = now - 2 * ttl
            protect = (spec or {}).get("protect_where")
            q = f"DELETE FROM {table} WHERE fetched_at < ?"
            if protect:
                q += f" AND NOT ({protect})"
            try:
                deleted += self._conn.execute(q, (cutoff,)).rowcount
            except sqlite3.OperationalError:
                continue  # no fetched_at column
        self._conn.commit()
        return deleted

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
