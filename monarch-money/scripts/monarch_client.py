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
import json
import os
import sqlite3
import tempfile
import time


# --------------------------------------------------------------------------- #
# Error classes
# --------------------------------------------------------------------------- #
class MonarchSkillError(Exception):
    """Base for the three fatal, non-retryable error classes."""


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
                        "giving account")


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
    # purposes, whatever the subtype claims (often plain 'brokerage').
    for kw in _CHARITABLE_KEYWORDS:
        if kw in n:
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
# Schema
# --------------------------------------------------------------------------- #
SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    id              TEXT PRIMARY KEY,
    name            TEXT,
    type            TEXT,
    subtype         TEXT,
    institution     TEXT,
    balance         REAL,
    display_balance REAL,
    currency        TEXT DEFAULT 'USD',
    is_manual       INTEGER,
    is_hidden       INTEGER,
    is_closed       INTEGER,
    updated_at      INTEGER,
    fetched_at      INTEGER
);

CREATE TABLE IF NOT EXISTS account_tax_buckets (
    account_id      TEXT PRIMARY KEY,
    tax_bucket      TEXT,
    auto_classified INTEGER DEFAULT 1,
    subtype_source  TEXT
);

CREATE TABLE IF NOT EXISTS holdings (
    account_id      TEXT,
    ticker          TEXT,
    name            TEXT,
    quantity        REAL,
    value           REAL,
    price           REAL,
    cost_basis      REAL,
    fetched_at      INTEGER,
    PRIMARY KEY (account_id, ticker)
);

CREATE TABLE IF NOT EXISTS transactions (
    id              TEXT PRIMARY KEY,
    account_id      TEXT,
    date            TEXT,
    amount          REAL,
    merchant        TEXT,
    category        TEXT,
    category_group  TEXT,
    tags            TEXT,
    notes           TEXT,
    is_pending      INTEGER,
    fetched_at      INTEGER
);
CREATE INDEX IF NOT EXISTS tx_date     ON transactions(date);
CREATE INDEX IF NOT EXISTS tx_account  ON transactions(account_id);
CREATE INDEX IF NOT EXISTS tx_category ON transactions(category);

CREATE TABLE IF NOT EXISTS budgets (
    period          TEXT,
    category_id     TEXT,
    category        TEXT,
    category_group  TEXT,
    budgeted        REAL,
    actual          REAL,
    fetched_at      INTEGER,
    PRIMARY KEY (period, category_id)
);

CREATE TABLE IF NOT EXISTS cashflow (
    period           TEXT PRIMARY KEY,
    income           REAL,
    expense          REAL,
    savings          REAL,
    savings_rate     REAL,
    by_category_json TEXT,
    by_group_json    TEXT,
    fetched_at       INTEGER
);

CREATE TABLE IF NOT EXISTS recurring (
    id              TEXT PRIMARY KEY,
    merchant        TEXT,
    amount          REAL,
    frequency       TEXT,
    next_date       TEXT,
    account_id      TEXT,
    fetched_at      INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
    key             TEXT PRIMARY KEY,
    value           TEXT
);
"""

# Per-table TTLs (seconds) used by prune_stale's 2x safety margin.
_TABLE_TTL = {
    "accounts": 4 * 3600,
    "holdings": 4 * 3600,
    "recurring": 4 * 3600,
}


class MonarchCache:
    """SQLite-backed offline cache for Monarch financial data."""

    def __init__(self, db_path):
        self.db_path = db_path
        if db_path != ":memory:":
            self.db_path = self._ensure_writable(db_path)
        self._conn = sqlite3.connect(self.db_path, timeout=5.0)
        self._conn.executescript(SCHEMA_SQL)
        self._set_meta("schema_version", str(SCHEMA_VERSION))
        self._conn.commit()

    # ---- writability -------------------------------------------------- #
    @staticmethod
    def _fallback_path():
        stamp = time.strftime("%Y%m%d")
        return os.path.join(tempfile.gettempdir(), f"monarch_cache_{stamp}.db")

    def _ensure_writable(self, db_path):
        """Try to create the parent dir; on PermissionError fall back to temp."""
        parent = os.path.dirname(os.path.abspath(db_path))
        try:
            os.makedirs(parent, exist_ok=True)
            return db_path
        except PermissionError:
            return self._fallback_path()

    # ---- meta helpers ------------------------------------------------- #
    def _set_meta(self, key, value):
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def _get_meta(self, key):
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    # ---- freshness ---------------------------------------------------- #
    def is_fresh(self, table, key_col, key_val, ttl_s):
        """True iff the newest matching row's fetched_at is within ttl_s."""
        row = self._conn.execute(
            f"SELECT MAX(fetched_at) FROM {table} WHERE {key_col} = ?",
            (key_val,),
        ).fetchone()
        if not row or row[0] is None:
            return False
        return (time.time() - row[0]) < ttl_s

    def any_fresh(self, table, ttl_s):
        """True iff the table has at least one row newer than ttl_s."""
        row = self._conn.execute(
            f"SELECT MAX(fetched_at) FROM {table}"
        ).fetchone()
        if not row or row[0] is None:
            return False
        return (time.time() - row[0]) < ttl_s

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
        """INSERT OR IGNORE — incremental-safe, never overwrites or deletes."""
        now = int(time.time())
        for t in rows:
            tags = t.get("tags", [])
            self._conn.execute(
                "INSERT OR IGNORE INTO transactions "
                "(id, account_id, date, amount, merchant, category, category_group, "
                " tags, notes, is_pending, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    t["id"], t.get("account_id"), t.get("date"), t.get("amount"),
                    t.get("merchant"), t.get("category"), t.get("category_group"),
                    json.dumps(tags), t.get("notes", ""),
                    int(bool(t.get("is_pending", 0))), now,
                ),
            )
        self._conn.commit()

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

    # ---- tx sync marker & counters ------------------------------------ #
    def get_last_tx_sync(self):
        return self._get_meta("last_tx_sync")

    def set_last_tx_sync(self, date_str):
        self._set_meta("last_tx_sync", date_str)
        self._conn.commit()

    def set_last_full_sync(self, date_str):
        self._set_meta("last_full_sync", date_str)
        self._conn.commit()

    def increment_api_calls(self, n=1):
        current = int(self._get_meta("api_call_count") or 0)
        self._set_meta("api_call_count", current + n)
        self._conn.commit()

    # ---- status & maintenance ----------------------------------------- #
    def status(self):
        c = self._conn
        out = {}
        for table in ("accounts", "holdings", "transactions", "budgets",
                      "cashflow", "recurring"):
            out[f"{table}_rows"] = c.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            age = c.execute(
                f"SELECT MAX(fetched_at) FROM {table}"
            ).fetchone()[0]
            out[f"{table}_last_sync"] = age
        out["api_call_count"] = int(self._get_meta("api_call_count") or 0)
        out["last_full_sync"] = self._get_meta("last_full_sync")
        out["last_tx_sync"] = self._get_meta("last_tx_sync")
        out["schema_version"] = self._get_meta("schema_version")
        if self.db_path != ":memory:" and os.path.exists(self.db_path):
            out["db_size_bytes"] = os.path.getsize(self.db_path)
        else:
            out["db_size_bytes"] = 0
        out["db_path"] = self.db_path
        return out

    def prune_stale(self):
        """Delete volatile rows older than 2x their TTL. Historical tables
        (transactions, budgets, cashflow) are intentionally left untouched."""
        now = int(time.time())
        deleted = 0
        for table, ttl in _TABLE_TTL.items():
            cutoff = now - 2 * ttl
            deleted += self._conn.execute(
                f"DELETE FROM {table} WHERE fetched_at < ?", (cutoff,)
            ).rowcount
        self._conn.commit()
        return deleted

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


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
DEFAULT_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
DEFAULT_SESSION_PATH = os.path.join(DEFAULT_DATA_DIR, "session.pickle")
DEFAULT_CRED_PATH = os.path.join(DEFAULT_DATA_DIR, "credentials.json")
DEFAULT_COOKIE_PATH = os.path.join(DEFAULT_DATA_DIR, "cookie_session.json")
DEFAULT_DB_PATH = os.path.join(DEFAULT_DATA_DIR, "monarch_cache.db")

MONARCH_ORIGIN = "https://app.monarch.com"


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
})


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

    def __getattr__(self, name):
        if name in FORBIDDEN_METHODS:
            raise BlockedEndpointError(
                f"Monarch endpoint '{name}' is blocked: the monarch-money "
                "skill never creates or deletes accounts. The user must do "
                "this in the Monarch app directly."
            )
        if name in CONFIRM_METHODS and name not in self._armed:
            raise ConfirmationRequiredError(
                f"Monarch endpoint '{name}' modifies the user's data. Get the "
                "user's explicit confirmation of this specific action in the "
                "conversation first, then wrap the call: "
                f"with client.mm.allow('{name}'): client.mm.{name}(...)"
            )
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

    @classmethod
    def from_session(cls, session_path=DEFAULT_SESSION_PATH,
                     cache_path=DEFAULT_DB_PATH):
        from monarchmoney import MonarchMoney
        if not os.path.exists(session_path):
            raise AuthExpiredError(
                "No saved Monarch session. Run scripts/setup_auth.py first."
            )
        mm = MonarchMoney(session_file=session_path)
        try:
            mm.load_session(session_path)
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
    def ensure_authenticated(cls, session_path=DEFAULT_SESSION_PATH,
                             cred_path=DEFAULT_CRED_PATH,
                             cache_path=DEFAULT_DB_PATH,
                             cookie_path=DEFAULT_COOKIE_PATH):
        """Try a saved token session, then a saved cookie session, then
        persisted credentials; else raise AuthExpiredError. Never retries a
        dead session."""
        try:
            return cls.from_session(session_path, cache_path)
        except MonarchSkillError:
            pass
        try:
            return cls.from_cookie_session(cookie_path, cache_path)
        except MonarchSkillError:
            pass
        if os.path.exists(cred_path):
            return cls.from_credentials(cred_path, cache_path, session_path)
        raise AuthExpiredError(
            "Monarch session expired. Re-run: python scripts/setup_auth.py "
            "--email ... --mfa-code CODE (or --token / --csrf-token + "
            "--session-id)"
        )
