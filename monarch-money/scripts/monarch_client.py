"""
monarch_client — SQLite offline cache, client bootstrap, and error classification
for the monarch-money skill.

Two public surfaces:

* ``MonarchCache`` — SQLite-backed store for accounts, holdings, transactions,
  budgets, cashflow, recurring, plus auto-classified tax buckets and a meta
  key/value table. Mirrors the ForecastSkill cache pattern (executescript
  schema, INSERT OR REPLACE upserts, persistent counters in a meta table).

* ``MonarchClient`` — thin wrapper that loads a saved Monarch session (or, when
  the user has opted in, persisted credentials), and classifies library
  exceptions into the skill's three fatal error classes.

Nothing here ever prints a password, session token, or MFA secret to stdout.
"""
import datetime as _dt
import functools
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Shared skeleton machinery (claude-skill-skeleton) — improve upstream, then
# `git fetch skeleton && git merge skeleton/main`.
from skill_cache import SkillCache                       # noqa: E402
from skill_common import (                               # noqa: E402
    SkillError, bootstrap_data, load_dotenv,
)
# Family contract: the cache + profile live in the family shared dir, not
# monarch's own skill-named dir (so personal-finance / investing find them).
from cache_contract import shared_data_dir               # noqa: E402
# Per-skill schema + version (skill_schema.py is monarch's, not skeleton's).
from skill_schema import SKILL_VERSION, SCHEMA, V2_DDL, V3_DDL   # noqa: E402,F401
# Write endpoints the library doesn't wrap, exposed through GuardedMonarch.
import monarch_writes as _WRITES                         # noqa: E402


# --------------------------------------------------------------------------- #
# Error classes
# --------------------------------------------------------------------------- #
class MonarchSkillError(SkillError):
    """Base for the fatal, non-retryable error classes (skeleton SkillError)."""


class AuthExpiredError(MonarchSkillError):
    """Monarch session is dead / 401. Re-run setup_auth.py with a fresh code."""


class SchemaChangedError(MonarchSkillError):
    """Monarch GraphQL schema changed under us — upgrade the library."""


class NetworkError(MonarchSkillError):
    """Cannot reach api.monarch.com (DNS / timeout / connection refused)."""


class BlockedEndpointError(MonarchSkillError):
    """An account-lifecycle endpoint was called. These are never allowed."""


class ConfirmationRequiredError(MonarchSkillError):
    """A write endpoint was called without explicit user confirmation."""


# --------------------------------------------------------------------------- #
# Tax-bucket inference
# --------------------------------------------------------------------------- #
# Order matters: "roth ira" must match roth before the bare "ira" traditional
# rule, and "traditional ira" must match traditional before "ira".
# Live Monarch subtypes are snake_case (health_savings_account,
# non_taxable_brokerage_account) — underscores are normalised to spaces first.
_TRAD_KEYWORDS = (
    "traditional", "401k", "403b", "457b", "sep ira", "simple ira", "pension",
)
_HSA_KEYWORDS = ("hsa", "health savings")
_EDU_KEYWORDS = ("529", "education")
_TAXABLE_KEYWORDS = ("brokerage", "individual", "joint", "taxable",
                     "investment", "trust")
_CHARITABLE_KEYWORDS = ("charitable", "daffy", "donor advised", "donor-advised",
                        "donor-advised fund", "giving account")
# `daf` needs a word boundary so it doesn't match "default"/"daffodil"; matched
# separately from the substring list. (A3)
_DAF_WORD = re.compile(r"\bdaf\b", re.IGNORECASE)


def infer_tax_bucket(subtype, name=None):
    """Map a Monarch account subtype (and, for ambiguous subtypes, the account
    name) to a tax bucket.

    Returns one of: roth | traditional | hsa | taxable | excluded | other.
    'excluded' marks accounts that belong in no tax bucket (charitable /
    donor-advised funds) — kept out of asset-location math without triggering
    the unclassified warning. A NULL/empty subtype is 'other' (ambiguous —
    needs user classification).
    """
    s = (subtype or "").lower().replace("_", " ").strip()
    n = (name or "").lower()
    # Charitable / donor-advised funds: not the user's assets for tax
    # purposes, whatever the subtype/type claims (Daffy syncs as name
    # "CHARITABLE FUND (...)", type "Other"). This name check runs for ALL
    # account types, before any subtype logic. (A3)
    if any(kw in n for kw in _CHARITABLE_KEYWORDS) or _DAF_WORD.search(n):
        return "excluded"
    if not s:
        return "other"
    if "roth" in s:
        return "roth"
    # Employer-plan brokerage windows (e.g. Fidelity BrokerageLink) carry the
    # subtype non_taxable_brokerage_account — the NAME disambiguates the
    # Roth window from the traditional one (QA field finding).
    if "non taxable" in s or "nontaxable" in s:
        return "roth" if "roth" in n else "traditional"
    for kw in _TRAD_KEYWORDS:
        if kw in s:
            return "traditional"
    if "ira" in s:  # bare IRA (already excluded roth above) → traditional
        return "traditional"
    for kw in _HSA_KEYWORDS:
        if kw in s:
            return "hsa"
    for kw in _EDU_KEYWORDS:
        if kw in s:
            return "other"
    for kw in _TAXABLE_KEYWORDS:
        if kw in s:
            return "taxable"
    return "other"


# --------------------------------------------------------------------------- #
# Exclusion period normalisation
# --------------------------------------------------------------------------- #
def _expand_period_bound(value, is_end):
    """Normalise a YYYY-MM period to a full ISO date bound — first day for a
    start, last day for an end. Full ISO dates and None/'' pass through.

    Exclusions store full ISO dates so the lexical comparisons in
    get_exclusion_filter_sql stay correct: an end bound of '2024-03' would
    otherwise be lexically < '2024-03-15' and wrongly exclude nothing."""
    if not value:
        return value
    v = str(value).strip()
    if len(v) == 7 and v[4] == "-" and v[:4].isdigit() and v[5:7].isdigit():
        year, month = int(v[:4]), int(v[5:7])
        if is_end:
            nxt = (_dt.date(year + 1, 1, 1) if month == 12
                   else _dt.date(year, month + 1, 1))
            return (nxt - _dt.timedelta(days=1)).isoformat()
        return _dt.date(year, month, 1).isoformat()
    return v


class MonarchCache(SkillCache):
    """Monarch's cache: skeleton SkillCache (schema-driven storage, TTL
    freshness, counters, status, schema-driven clear/prune) plus the
    monarch-specific write methods and tax-bucket logic below."""

    def __init__(self, db_path):
        # Capture the on-disk schema_version BEFORE the parent stamps the new
        # one, so _migrate_schema can tell a fresh/v2 db from an upgraded v1.
        prior = self._read_prior_version(db_path)
        super().__init__(db_path, SCHEMA)
        self._migrate_schema(prior)

    # ---- schema migration --------------------------------------------- #
    @staticmethod
    def _read_prior_version(db_path):
        """Return the schema_version stored in an existing db file, or None for
        a brand-new (or in-memory) database. Best-effort: any read failure is
        treated as 'no prior version'."""
        if db_path == ":memory:" or not os.path.exists(db_path):
            return None
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()
            finally:
                conn.close()
            return int(row[0]) if row and row[0] is not None else None
        except (sqlite3.Error, ValueError):
            return None

    def _migrate_schema(self, prior_version):
        """Run on every cache init. SkillCache already executed the full DDL
        (all CREATE ... IF NOT EXISTS, including the v2 tables) and stamped the
        current schema_version. This makes the v1->v2 transition explicit and
        idempotent: a database that predates v2 gets the new snapshot tables
        ensured here, so the upgrade is driven by the version marker rather
        than relying solely on the union DDL.

        prior_version is None for a fresh/in-memory db (nothing to migrate)."""
        if prior_version is None:
            return
        if prior_version < 2:
            self._conn.executescript(V2_DDL)
        if prior_version < 3:
            self._conn.executescript(V3_DDL)
        self._conn.commit()

    # ---- accounts ----------------------------------------------------- #
    def write_accounts(self, rows):
        now = int(time.time())
        for a in rows:
            self._conn.execute(
                "INSERT OR REPLACE INTO accounts "
                "(id, name, type, subtype, institution, balance, display_balance, "
                " currency, is_manual, is_hidden, is_closed, updated_at, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    a["id"], a.get("name"), a.get("type"), a.get("subtype"),
                    a.get("institution"), a.get("balance"),
                    a.get("display_balance", a.get("balance")),
                    a.get("currency", "USD"),
                    int(bool(a.get("is_manual", 0))),
                    int(bool(a.get("is_hidden", 0))),
                    int(bool(a.get("is_closed", 0))),
                    a.get("updated_at"), now,
                ),
            )
        self._conn.commit()
        self.auto_classify_tax_buckets(rows)

    def auto_classify_tax_buckets(self, account_rows):
        """Upsert a tax bucket per account. User corrections
        (auto_classified=0) are NEVER overwritten; auto rows are updated on
        every sync so classifier improvements heal stale classifications."""
        for a in account_rows:
            bucket = infer_tax_bucket(a.get("subtype"), a.get("name"))
            self._conn.execute(
                "INSERT INTO account_tax_buckets "
                "(account_id, tax_bucket, auto_classified, subtype_source) "
                "VALUES (?, ?, 1, ?) "
                "ON CONFLICT(account_id) DO UPDATE SET "
                "  tax_bucket = excluded.tax_bucket, "
                "  subtype_source = excluded.subtype_source "
                "WHERE account_tax_buckets.auto_classified = 1",
                (a["id"], bucket, a.get("subtype")),
            )
        self._conn.commit()

    def unclassified_investment_accounts(self):
        """Account ids that are investment-type but bucket='other'.

        Monarch's live type vocabulary uses 'brokerage' for investment
        accounts (QA field finding); 'investment' kept for fixtures/older data.
        """
        rows = self._conn.execute(
            "SELECT a.id FROM accounts a "
            "JOIN account_tax_buckets b ON a.id = b.account_id "
            "WHERE a.type IN ('investment','brokerage') "
            "AND b.tax_bucket = 'other'"
        ).fetchall()
        return [r[0] for r in rows]

    # ---- holdings ----------------------------------------------------- #
    def write_holdings(self, account_id, rows):
        now = int(time.time())
        self._conn.execute(
            "DELETE FROM holdings WHERE account_id = ?", (account_id,)
        )
        for h in rows:
            self._conn.execute(
                "INSERT OR REPLACE INTO holdings "
                "(account_id, ticker, name, quantity, value, price, cost_basis, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    account_id, h.get("ticker"), h.get("name"),
                    h.get("quantity"), h.get("value"), h.get("price"),
                    h.get("cost_basis"), now,
                ),
            )
        self._conn.commit()

    # ---- transactions ------------------------------------------------- #
    def write_transactions(self, rows):
        """Upsert by id — re-syncing a transaction refreshes it, so pending ->
        posted and recategorisations in Monarch reach the cache. Idempotent;
        never deletes (see prune_missing_pending)."""
        now = int(time.time())
        for t in rows:
            tags = t.get("tags", [])
            self._conn.execute(
                "INSERT INTO transactions "
                "(id, account_id, date, amount, merchant, category, category_group, "
                " tags, notes, is_pending, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "  account_id = excluded.account_id, date = excluded.date, "
                "  amount = excluded.amount, merchant = excluded.merchant, "
                "  category = excluded.category, "
                "  category_group = excluded.category_group, "
                "  tags = excluded.tags, notes = excluded.notes, "
                "  is_pending = excluded.is_pending, "
                "  fetched_at = excluded.fetched_at",
                (
                    t["id"], t.get("account_id"), t.get("date"), t.get("amount"),
                    t.get("merchant"), t.get("category"), t.get("category_group"),
                    json.dumps(tags), t.get("notes", ""),
                    int(bool(t.get("is_pending", 0))), now,
                ),
            )
        self._conn.commit()

    def prune_missing_pending(self, start_date, seen_ids):
        """Delete pending transactions dated >= start_date whose ids were not
        in a COMPLETE fetch of that window: they settled under a new id or
        were dropped upstream. Posted rows are never touched. Returns count."""
        seen = {i for i in seen_ids if i is not None}
        rows = self._conn.execute(
            "SELECT id FROM transactions WHERE is_pending = 1 AND date >= ?",
            (start_date,)).fetchall()
        stale = [r[0] for r in rows if r[0] not in seen]
        self._conn.executemany(
            "DELETE FROM transactions WHERE id = ?", [(i,) for i in stale])
        self._conn.commit()
        return len(stale)

    # ---- budgets ------------------------------------------------------ #
    def write_budgets(self, rows, period):
        now = int(time.time())
        for b in rows:
            self._conn.execute(
                "INSERT OR REPLACE INTO budgets "
                "(period, category_id, category, category_group, budgeted, actual, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    period, b.get("category_id"), b.get("category"),
                    b.get("category_group"), b.get("budgeted"), b.get("actual"), now,
                ),
            )
        self._conn.commit()

    # ---- cashflow ----------------------------------------------------- #
    def write_cashflow(self, summary, period):
        now = int(time.time())
        self._conn.execute(
            "INSERT OR REPLACE INTO cashflow "
            "(period, income, expense, savings, savings_rate, by_category_json, by_group_json, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                period, summary.get("income"), summary.get("expense"),
                summary.get("savings"), summary.get("savings_rate"),
                json.dumps(summary.get("by_category", {})),
                json.dumps(summary.get("by_group", {})),
                now,
            ),
        )
        self._conn.commit()

    # ---- recurring ---------------------------------------------------- #
    def write_recurring(self, rows):
        now = int(time.time())
        self._conn.execute("DELETE FROM recurring")
        for r in rows:
            self._conn.execute(
                "INSERT OR REPLACE INTO recurring "
                "(id, merchant, amount, frequency, next_date, account_id, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    r["id"], r.get("merchant"), r.get("amount"),
                    r.get("frequency"), r.get("next_date"), r.get("account_id"), now,
                ),
            )
        self._conn.commit()

    # ---- net worth history (v2) --------------------------------------- #
    def write_net_worth_history(self, rows):
        """Daily net-worth snapshots (one row per date). INSERT OR REPLACE so
        re-syncing a date overwrites it idempotently. Each row:
        {date, total, assets, liabilities}."""
        now = int(time.time())
        for r in rows:
            self._conn.execute(
                "INSERT OR REPLACE INTO net_worth_history "
                "(date, total, assets, liabilities, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    r.get("date"), r.get("total"), r.get("assets"),
                    r.get("liabilities"), now,
                ),
            )
        self._conn.commit()

    # ---- account balance history (v2) --------------------------------- #
    def write_account_history(self, account_id, rows):
        """Daily per-account balance snapshots. INSERT OR REPLACE keyed on
        (account_id, date) — idempotent. Each row: {date, balance}."""
        now = int(time.time())
        for r in rows:
            self._conn.execute(
                "INSERT OR REPLACE INTO account_history "
                "(account_id, date, balance, fetched_at) "
                "VALUES (?, ?, ?, ?)",
                (account_id, r.get("date"), r.get("balance"), now),
            )
        self._conn.commit()

    # ---- tx sync marker & counters (thin wrappers over skeleton meta) -- #
    def get_last_tx_sync(self):
        return self.get_meta("last_tx_sync")

    def set_last_tx_sync(self, date_str):
        self.set_meta("last_tx_sync", date_str)

    def set_last_full_sync(self, date_str):
        self.set_meta("last_full_sync", date_str)

    def increment_api_calls(self, n=1):
        self.increment_counter("api_call_count", n)

    # ---- status & maintenance (skeleton-backed) ------------------------ #
    def status(self):
        out = super().status()
        out["last_full_sync"] = self.get_meta("last_full_sync")
        out["last_tx_sync"] = self.get_meta("last_tx_sync")
        return out

    def clear(self, include_user_buckets=False):
        """Schema-driven clear (see skill_schema.SCHEMA): protected rows are
        user-corrected tax buckets (auto_classified=0); include_user_buckets
        maps to the skeleton's force flag. Returns counts keyed for fetch.py
        (tax_buckets / user_buckets_kept aliases)."""
        counts = super().clear(force=include_user_buckets)
        counts["tax_buckets"] = counts.get("account_tax_buckets", 0)
        counts["user_buckets_kept"] = counts.get("protected_rows_kept", 0)
        return counts

    # ---- exclusions (v3) ---------------------------------------------- #
    # User-defined filters for one-time / non-recurring expenses. NOT part of
    # the cache-lifecycle machinery (see skill_schema V3_DDL) — they survive a
    # clear-cache, since the API can't regenerate them.
    _EXCL_COLS = ("id", "type", "value", "start_date", "end_date", "reason",
                  "exclude_from", "auto_suggested", "created_at")

    def add_exclusion(self, type, value, start_date=None, end_date=None,
                      reason=None, exclude_from="all", auto_suggested=False):
        """Idempotent add; returns the rowid (the existing row's id on a
        duplicate). YYYY-MM bounds are expanded to full ISO dates so the filter
        SQL compares correctly.

        We dedupe with an explicit NULL-safe check rather than relying on the
        UNIQUE(type, value, start_date, end_date) constraint, because SQLite
        treats NULLs as distinct in UNIQUE indexes — so two all-time
        exclusions (start/end NULL) would otherwise both insert. The constraint
        stays as a backstop for fully-dated rows."""
        start_date = _expand_period_bound(start_date, is_end=False)
        end_date = _expand_period_bound(end_date, is_end=True)
        # NULL-safe lookup (SQLite `IS` matches NULLs and values alike).
        existing = self._conn.execute(
            "SELECT id FROM exclusions "
            "WHERE type IS ? AND value IS ? AND start_date IS ? AND end_date IS ?",
            (type, value, start_date, end_date),
        ).fetchone()
        if existing:
            return existing[0]
        cur = self._conn.execute(
            "INSERT INTO exclusions "
            "(type, value, start_date, end_date, reason, exclude_from, "
            " auto_suggested, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (type, value, start_date, end_date, reason, exclude_from,
             int(bool(auto_suggested)), int(time.time())),
        )
        self._conn.commit()
        return cur.lastrowid

    def remove_exclusion(self, exclusion_id):
        """DELETE by id; returns True iff a row existed."""
        cur = self._conn.execute(
            "DELETE FROM exclusions WHERE id = ?", (exclusion_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def list_exclusions(self, type_filter=None):
        """All exclusions as dicts (newest first), optionally one type only."""
        q = f"SELECT {', '.join(self._EXCL_COLS)} FROM exclusions"
        params = ()
        if type_filter:
            q += " WHERE type = ?"
            params = (type_filter,)
        q += " ORDER BY created_at DESC, id DESC"
        return [dict(zip(self._EXCL_COLS, r))
                for r in self._conn.execute(q, params).fetchall()]

    def clear_auto_exclusions(self):
        """Remove every skill-suggested exclusion (auto_suggested=1). Returns
        the count removed. User-added exclusions are untouched."""
        cur = self._conn.execute(
            "DELETE FROM exclusions WHERE auto_suggested = 1")
        self._conn.commit()
        return cur.rowcount

    def get_exclusion_filter_sql(self, scope, tx_table_alias="t"):
        """Return (sql_fragment, params) to AND into a WHERE clause querying
        the transactions table aliased as `tx_table_alias`. The fragment drops
        any transaction matched by an active exclusion whose exclude_from is
        'all' or equals `scope`.

        Single source of truth — every exclusion-aware caller (fetch.py and the
        downstream personal-finance / retirement skills) uses this so the
        filtering logic lives in exactly one place. Four NOT EXISTS clauses,
        one per exclusion type; each carries the scope param, so scope='all'
        yields exactly four params.

        NULL start/end mean all-time (COALESCE to open bounds). value/scope are
        always parameterised; only the developer-controlled table alias is
        interpolated."""
        t = tx_table_alias
        date_lo = f"({t}.date >= COALESCE(x.start_date, '0000-00-00'))"
        date_hi = f"({t}.date <= COALESCE(x.end_date, '9999-99-99'))"
        scope_ok = "(x.exclude_from = 'all' OR x.exclude_from = ?)"
        frag = (
            f" AND NOT EXISTS (SELECT 1 FROM exclusions x "
            f"WHERE x.type = 'transaction' AND x.value = {t}.id "
            f"AND {scope_ok})"
            f" AND NOT EXISTS (SELECT 1 FROM exclusions x "
            f"WHERE x.type = 'category' AND x.value = {t}.category "
            f"AND {date_lo} AND {date_hi} AND {scope_ok})"
            f" AND NOT EXISTS (SELECT 1 FROM exclusions x "
            f"WHERE x.type = 'merchant' AND x.value = {t}.merchant "
            f"AND {date_lo} AND {date_hi} AND {scope_ok})"
            f" AND NOT EXISTS (SELECT 1 FROM exclusions x "
            f"WHERE x.type = 'category_period' AND x.value = {t}.category "
            f"AND {date_lo} AND {date_hi} AND {scope_ok})"
        )
        return frag, [scope, scope, scope, scope]


# --------------------------------------------------------------------------- #
# Error classification
# --------------------------------------------------------------------------- #
def classify_error(exc):
    """Map an arbitrary library/runtime exception to one of the three fatal
    skill error classes. Used by both fetch.py and setup_auth.py."""
    # Already one of ours — pass through.
    if isinstance(exc, MonarchSkillError):
        return exc

    name = type(exc).__name__
    msg = str(exc)
    low = msg.lower()

    # Auth: library LoginFailedException, or HTTP 401/403.
    if name == "LoginFailedException" or "401" in msg or "unauthorized" in low \
            or "forbidden" in low or "login" in low and "fail" in low:
        return AuthExpiredError(
            "Monarch session expired. Re-run: python scripts/setup_auth.py "
            "--email ... --mfa-code CODE"
        )

    # Network: connection / timeout / DNS.
    if isinstance(exc, (ConnectionError, TimeoutError)) or any(
        k in low for k in ("timed out", "timeout", "connection",
                            "getaddrinfo", "name resolution", "unreachable")
    ):
        return NetworkError(
            "Cannot reach api.monarch.com. Check network egress from container."
        )

    # Schema drift: GraphQL parse / unexpected key.
    if isinstance(exc, (KeyError, TypeError)) or any(
        k in low for k in ("graphql", "keyerror", "field", "parse",
                           "schema", "validationerror", "transporterror")
    ):
        return SchemaChangedError(
            "Monarch API schema changed. Update: pip install -U monarchmoneycommunity"
        )

    # Default: treat unknown errors as schema drift (safest — surfaces a
    # library-upgrade hint rather than silently retrying).
    return SchemaChangedError(
        f"Unexpected Monarch error ({name}): {msg}. "
        "Try: pip install -U monarchmoneycommunity"
    )


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The family SHARED dir — monarch-money owns the location, so this resolves to
# monarch's own persistent dir by default ($MONARCH_MONEY_FAMILY_DIR >
# $MONARCH_MONEY_DATA_DIR > /mnt/user-data/.claude-skills/monarch-money (cloud) >
# ~/.claude-skills/monarch-money (desktop) > skill-relative data/). The cache +
# profile.json land here so personal-finance / investing read the SAME files;
# monarch's own auth (session/cookie/.env) rides along in the same dir.
DEFAULT_DATA_DIR = shared_data_dir(_SKILL_DIR)
# First-run bootstrap: auth files bundled via `build.ps1 -IncludeAuth` ride in
# the package's data/bootstrap/ and are copied into the persistent dir once.
bootstrap_data(DEFAULT_DATA_DIR, _SKILL_DIR)
DEFAULT_SESSION_PATH = os.path.join(DEFAULT_DATA_DIR, "session.pickle")
DEFAULT_CRED_PATH = os.path.join(DEFAULT_DATA_DIR, "credentials.json")
DEFAULT_COOKIE_PATH = os.path.join(DEFAULT_DATA_DIR, "cookie_session.json")
DEFAULT_ENV_PATH = os.path.join(DEFAULT_DATA_DIR, ".env")
DEFAULT_DB_PATH = os.path.join(DEFAULT_DATA_DIR, "monarch_cache.db")

# Where Monarch MCP servers conventionally drop their own session pickle. The
# robcerda/monarch-mcp-server saves `.mm/mm_session.pickle` relative to where
# login_setup.py ran — typically the MCP server's repo dir (which may sit
# beside this skill) or the user's home. We can share that session so MCP-mode
# users don't have to re-authenticate the skill separately.
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SKILL_DIR)
DEFAULT_MCP_REPO_SESSION = os.path.join(_REPO_ROOT, ".mm", "mm_session.pickle")
DEFAULT_HOME_MCP_SESSION = os.path.join(
    os.path.expanduser("~"), ".mm", "mm_session.pickle")

MONARCH_ORIGIN = "https://app.monarch.com"


def _config_session_path(start_dir):
    """Walk up from start_dir looking for a config.json carrying a
    `session_path` key; return that path (user-expanded) if it exists on disk,
    else None. Lets a user point the skill at an MCP session in a custom
    location without code changes."""
    d = os.path.abspath(start_dir)
    while True:
        cfg = os.path.join(d, "config.json")
        if os.path.isfile(cfg):
            try:
                with open(cfg, "r", encoding="utf-8") as fh:
                    sp = json.load(fh).get("session_path")
                if sp:
                    sp = os.path.expanduser(sp)
                    if os.path.exists(sp):
                        return sp
            except (OSError, ValueError):
                pass
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _find_session(session_path=DEFAULT_SESSION_PATH,
                  mcp_repo_session=DEFAULT_MCP_REPO_SESSION,
                  home_mcp_session=DEFAULT_HOME_MCP_SESSION,
                  config_search_dir=_SCRIPTS_DIR):
    """Locate a usable Monarch session pickle, in priority order:

      1. ``session_path`` — the skill's own saved session (persistent data dir)
      2. ``<repo>/.mm/mm_session.pickle`` — an MCP server run in/beside the repo
      3. ``~/.mm/mm_session.pickle`` — an MCP server installed elsewhere
      4. a ``session_path`` from a ``config.json`` found walking up from the
         scripts dir

    Returns the first path that exists, or None if nothing is found. The
    caller logs the chosen path to STDERR (never stdout — keeps machine-
    parseable Markdown/JSON clean)."""
    for candidate in (session_path, mcp_repo_session, home_mcp_session):
        if candidate and os.path.exists(candidate):
            return candidate
    return _config_session_path(config_search_dir)


def apply_cookie_auth(mm, csrftoken, session_id):
    """Configure a MonarchMoney instance for cookie auth.

    monarchmoneycommunity >= 1.4.0 supports cookies natively (set_cookies
    sets the Cookie / X-Csrftoken / Origin headers and auth_mode='cookie',
    which save_session/load_session persist). The manual-header fallback
    covers older library versions. Monarch's cookie names are `session_id`
    and `csrftoken` (REQUIRED_COOKIES).
    """
    cookies = {"session_id": session_id, "csrftoken": csrftoken}
    if hasattr(mm, "set_cookies"):
        mm.set_cookies(cookies)
    else:  # pre-1.4 fallback: hand-build the headers
        mm._headers["Cookie"] = f"csrftoken={csrftoken}; session_id={session_id}"
        mm._headers["X-CSRFToken"] = csrftoken
        mm._headers["Origin"] = MONARCH_ORIGIN
        mm._headers["Referer"] = MONARCH_ORIGIN + "/"
    return mm


# Two-tier write policy, enforced in code AND mirrored in SKILL.md:
#
# FORBIDDEN_METHODS — account lifecycle. Never callable, no override exists.
# CONFIRM_METHODS   — all other write endpoints. Callable ONLY inside a
#                     `with client.mm.allow("<method>"):` block, which Claude
#                     may open only after the user explicitly confirmed that
#                     specific action in the conversation.
FORBIDDEN_METHODS = frozenset({
    "create_manual_account",
    "delete_account",
})

CONFIRM_METHODS = frozenset({
    # accounts (non-lifecycle)
    "update_account",
    "request_accounts_refresh",
    "request_accounts_refresh_and_wait",
    # transactions
    "create_transaction",
    "delete_transaction",
    "update_transaction",
    "update_transaction_splits",
    "set_transaction_tags",
    "create_transaction_tag",
    "create_transaction_category",
    "delete_transaction_category",
    "delete_transaction_categories",
    # budgets
    "set_budget_amount",
    "reset_budget",
    "update_flexible_budget",
    "update_flex_rollover_settings",
    # recurring / uploads
    "update_reoccuring",
    "upload_account_balance_history",
    "upload_attachment",
    # Not in the pinned library, but newer forks ship it; gate it so an
    # upgrade can't expose an ungated write.
    "upload_receipt_to_inbox",
    # skill-implemented writes (monarch_writes.WRITE_METHODS)
    "create_transaction_rule",
    "update_transaction_rule",
    "delete_transaction_rule",
    "rename_merchant",
    "mark_stream_as_not_recurring",
    "review_recurring_stream",
    "update_transaction_category",
    # a raw GraphQL MUTATION (queries through gql_call stay ungated)
    "gql_call",
})


def _is_mutation(graphql_query):
    """True if a gql() document (gql 3 DocumentNode or gql 4 GraphQLRequest)
    contains a mutation operation. Unparseable input counts as a mutation."""
    doc = getattr(graphql_query, "document", graphql_query)
    try:
        return any(getattr(getattr(d, "operation", None), "value", None)
                   == "mutation" for d in doc.definitions)
    except AttributeError:
        return True


class _Armed:
    """Context manager that arms exactly one confirmed write method."""

    def __init__(self, guard, name):
        self._guard = guard
        self._name = name

    def __enter__(self):
        self._guard._armed.add(self._name)
        return self._guard

    def __exit__(self, *exc):
        self._guard._armed.discard(self._name)
        return False


class GuardedMonarch:
    """Proxy around MonarchMoney enforcing the write policy.

    Reads and internal attributes (_headers, token, save_session, …) pass
    through untouched. FORBIDDEN_METHODS raise BlockedEndpointError at
    attribute lookup — before any request exists — and cannot be armed.
    CONFIRM_METHODS raise ConfirmationRequiredError unless called inside
    `with guard.allow("<method>"):`, which represents the user's explicit,
    per-action confirmation in the conversation.
    """

    def __init__(self, mm):
        self._mm = mm
        self._armed = set()

    def allow(self, name):
        """Arm one user-confirmed write method for the duration of a
        with-block. Only call this AFTER the user explicitly confirmed the
        specific action in chat."""
        if name in FORBIDDEN_METHODS:
            raise BlockedEndpointError(
                f"Monarch endpoint '{name}' can never be armed: account "
                "create/delete is permanently blocked. The user must do this "
                "in the Monarch app directly."
            )
        if name not in CONFIRM_METHODS:
            raise ValueError(
                f"'{name}' is not a gated write method; call it directly."
            )
        return _Armed(self, name)

    def _require_armed(self, name):
        if name not in self._armed:
            raise ConfirmationRequiredError(
                f"Monarch endpoint '{name}' modifies the user's data. Get the "
                "user's explicit confirmation of this specific action in the "
                "conversation first, then wrap the call: "
                f"with client.mm.allow('{name}'): client.mm.{name}(...)"
            )

    def gql_call(self, operation, graphql_query, variables=None):
        """Raw GraphQL passthrough. Queries are free; a mutation needs
        allow('gql_call') — otherwise any write could skip the gate."""
        if _is_mutation(graphql_query):
            self._require_armed("gql_call")
        return self._mm.gql_call(operation=operation,
                                 graphql_query=graphql_query,
                                 variables=variables or {})

    def __getattr__(self, name):
        if name in FORBIDDEN_METHODS:
            raise BlockedEndpointError(
                f"Monarch endpoint '{name}' is blocked: the monarch-money "
                "skill never creates or deletes accounts. The user must do "
                "this in the Monarch app directly."
            )
        if name in CONFIRM_METHODS:
            self._require_armed(name)
        # Skill-implemented endpoints the library lacks. They run against the
        # RAW client, so their internal gql_call is not double-gated.
        impl = _WRITES.WRITE_METHODS.get(name) or _WRITES.READ_METHODS.get(name)
        if impl is not None:
            return functools.partial(impl, self._mm)
        return getattr(self._mm, name)


# Backwards-compatible alias (pre-write-policy name).
ReadOnlyMonarch = GuardedMonarch


class MonarchClient:
    """Wraps a logged-in MonarchMoney instance plus the cache.

    The instance is always wrapped in GuardedMonarch — account lifecycle is
    never callable and all other writes require per-action arming, no matter
    how the client was constructed.
    """

    def __init__(self, mm_instance, cache):
        if not isinstance(mm_instance, GuardedMonarch):
            mm_instance = GuardedMonarch(mm_instance)
        self.mm = mm_instance
        self.cache = cache
        self.auth_source = None  # set by ensure_authenticated

    @classmethod
    def from_session(cls, session_path=DEFAULT_SESSION_PATH,
                     cache_path=DEFAULT_DB_PATH):
        from monarchmoney import MonarchMoney
        found = _find_session(session_path)
        if found is None:
            raise AuthExpiredError(
                "No saved Monarch session. Run scripts/setup_auth.py first."
            )
        if found != session_path:
            # MCP-shared session — note where it came from (stderr, not stdout).
            print(f"[monarch] using shared session pickle at {found}",
                  file=sys.stderr)
        mm = MonarchMoney(session_file=found)
        try:
            mm.load_session(found)
        except Exception as exc:  # noqa: BLE001
            # A session that won't load (corrupt / expired / wrong format) is an
            # auth problem, not schema drift — re-authentication is the fix.
            raise AuthExpiredError(
                "Saved Monarch session is unreadable or expired. Re-run: "
                "python scripts/setup_auth.py --email ... --mfa-code CODE"
            ) from exc
        return cls(mm, MonarchCache(cache_path))

    @classmethod
    def from_credentials(cls, cred_path=DEFAULT_CRED_PATH,
                         cache_path=DEFAULT_DB_PATH,
                         session_path=DEFAULT_SESSION_PATH):
        import asyncio
        from monarchmoney import MonarchMoney
        if not os.path.exists(cred_path):
            raise AuthExpiredError("No persisted credentials (Option B not enabled).")
        with open(cred_path, "r", encoding="utf-8") as fh:
            creds = json.load(fh)
        mm = MonarchMoney(session_file=session_path)
        mfa_secret = creds.get("mfa_secret_key")
        try:
            asyncio.run(mm.login(
                email=creds.get("email"),
                password=creds.get("password"),
                use_saved_session=False,
                save_session=False,
                mfa_secret_key=mfa_secret,
            ))
            mm.save_session(session_path)
        except Exception as exc:  # noqa: BLE001
            raise classify_error(exc)
        return cls(mm, MonarchCache(cache_path))

    @classmethod
    def from_cookie_session(cls, cookie_path=DEFAULT_COOKIE_PATH,
                            cache_path=DEFAULT_DB_PATH):
        """Legacy fallback: cookies saved by pre-1.4 setups in
        data/cookie_session.json. New cookie sessions persist in the standard
        session pickle (library 1.4.0 saves auth_mode + cookies natively) and
        load via from_session.
        """
        from monarchmoney import MonarchMoney
        if not os.path.exists(cookie_path):
            raise AuthExpiredError("No saved cookie session.")
        try:
            with open(cookie_path, "r", encoding="utf-8") as fh:
                cookies = json.load(fh)
            csrftoken = cookies["csrftoken"]
            # Older files used Django's default name; Monarch's is session_id.
            session_id = cookies.get("session_id", cookies.get("sessionid"))
            if not session_id:
                raise KeyError("session_id")
        except (json.JSONDecodeError, KeyError) as exc:
            raise AuthExpiredError(
                "Saved cookie session is unreadable. Re-run: python "
                "scripts/setup_auth.py --csrf-token ... --session-id ..."
            ) from exc
        mm = MonarchMoney()
        apply_cookie_auth(mm, csrftoken, session_id)
        return cls(mm, MonarchCache(cache_path))

    @classmethod
    def from_env(cls, env_path=DEFAULT_ENV_PATH, cache_path=DEFAULT_DB_PATH,
                 session_path=DEFAULT_SESSION_PATH):
        """Authenticate from a .env file in the persistent data dir — the
        zero-touch desktop pattern: each new chat session reads it and
        re-auths without asking. Keys (either group):
          MONARCH_COOKIES="session_id=...; csrftoken=..."   (preferred)
          MONARCH_EMAIL / MONARCH_PASSWORD / MONARCH_MFA_SECRET
        """
        import asyncio
        from monarchmoney import MonarchMoney
        env = load_dotenv(env_path)
        if not env:
            raise AuthExpiredError("No .env auth file.")
        mm = MonarchMoney(session_file=session_path)
        cookies = env.get("MONARCH_COOKIES")
        if cookies:
            pairs = {}
            for part in cookies.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    pairs[k.strip()] = v.strip()
            session_id = pairs.get("session_id", pairs.get("sessionid"))
            csrftoken = pairs.get("csrftoken")
            if not (session_id and csrftoken):
                raise AuthExpiredError(
                    "MONARCH_COOKIES in .env must contain session_id and csrftoken.")
            apply_cookie_auth(mm, csrftoken, session_id)
            return cls(mm, MonarchCache(cache_path))
        email = env.get("MONARCH_EMAIL")
        password = env.get("MONARCH_PASSWORD")
        if not (email and password):
            raise AuthExpiredError(".env has no usable Monarch auth keys.")
        try:
            asyncio.run(mm.login(
                email=email, password=password,
                use_saved_session=False, save_session=False,
                mfa_secret_key=env.get("MONARCH_MFA_SECRET"),
            ))
            mm.save_session(session_path)
        except Exception as exc:  # noqa: BLE001
            raise classify_error(exc)
        return cls(mm, MonarchCache(cache_path))

    @classmethod
    def from_vault(cls, cache_path=DEFAULT_DB_PATH,
                   session_path=DEFAULT_SESSION_PATH):
        """Authenticate from the **secrets** vault, if installed. Reads
        the `monarch_cookies` secret IN-PROCESS via the shared secret_provider
        contract (never printed) and adopts it as a cookie session. This lets
        the credential live centrally in one vault instead of being bundled into
        this skill — monarch consults the vault as a fallback."""
        try:
            from secret_provider import get_secret
        except ImportError:
            raise AuthExpiredError("secret_provider not vendored.")
        cookies = get_secret("monarch_cookies")
        if not cookies:
            raise AuthExpiredError("No monarch_cookies in the secrets vault.")
        from monarchmoney import MonarchMoney
        pairs = {}
        for part in cookies.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                pairs[k.strip()] = v.strip()
        session_id = pairs.get("session_id", pairs.get("sessionid"))
        csrftoken = pairs.get("csrftoken")
        if not (session_id and csrftoken):
            raise AuthExpiredError(
                "vault monarch_cookies missing session_id/csrftoken.")
        mm = MonarchMoney(session_file=session_path)
        apply_cookie_auth(mm, csrftoken, session_id)
        return cls(mm, MonarchCache(cache_path))

    @classmethod
    def ensure_authenticated(cls, session_path=DEFAULT_SESSION_PATH,
                             cred_path=DEFAULT_CRED_PATH,
                             cache_path=DEFAULT_DB_PATH,
                             cookie_path=DEFAULT_COOKIE_PATH,
                             env_path=DEFAULT_ENV_PATH,
                             skip_session=False):
        """Try a saved token session, then a saved cookie session, then
        persisted credentials, then the .env file, then the secrets
        vault; else raise AuthExpiredError. Never retries a dead session.

        skip_session=True skips the saved session pickle — used once by
        fetch.py after the server rejected that pickle. The returned client's
        `auth_source` names the source that succeeded."""
        def _tagged(client, source):
            client.auth_source = source
            return client

        if not skip_session:
            try:
                return _tagged(cls.from_session(session_path, cache_path),
                               "session")
            except MonarchSkillError:
                pass
        try:
            return _tagged(cls.from_cookie_session(cookie_path, cache_path),
                           "cookie")
        except MonarchSkillError:
            pass
        if os.path.exists(cred_path):
            return _tagged(cls.from_credentials(cred_path, cache_path,
                                                session_path), "credentials")
        try:
            return _tagged(cls.from_env(env_path, cache_path, session_path),
                           "env")
        except MonarchSkillError:
            pass
        try:
            return _tagged(cls.from_vault(cache_path, session_path), "vault")
        except MonarchSkillError:
            pass
        raise AuthExpiredError(
            "Monarch session expired. Re-run: python scripts/setup_auth.py "
            "--email ... --mfa-code CODE (or --token / --csrf-token + "
            "--session-id), put MONARCH_COOKIES in "
            f"{env_path} for zero-touch reauth, or install the **secrets** "
            "vault with a `monarch_cookies` secret."
        )
