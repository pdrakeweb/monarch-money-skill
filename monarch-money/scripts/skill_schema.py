"""
skill_schema — monarch-money's per-skill definition for the shared skeleton
machinery (skill_cache.SkillCache). PER-SKILL FILE: this is where monarch
diverges from claude-skill-skeleton by design.
"""

# Bump on EVERY commit that changes this skill (see CLAUDE.md). Must match
# the `version:` in SKILL.md frontmatter — builds refuse on mismatch.
SKILL_VERSION = "1.4.2"

TTL_4H = 4 * 3600
TTL_24H = 24 * 3600

# v2 (schema_version 2) tables — daily snapshot history unlocked by MCP mode
# (get_net_worth / get_account_balance_history expose series fetch.py never
# pulled). Split out so MonarchCache._migrate_schema can (re)create exactly
# these when upgrading a v1 database, and so the base DDL stays the union of
# all versions for fresh installs. All CREATE ... IF NOT EXISTS — idempotent.
V2_DDL = """
CREATE TABLE IF NOT EXISTS net_worth_history (
    date            TEXT PRIMARY KEY,   -- ISO-8601 date
    total           REAL,
    assets          REAL,
    liabilities     REAL,
    fetched_at      INTEGER
);

CREATE TABLE IF NOT EXISTS account_history (
    account_id      TEXT,
    date            TEXT,               -- ISO-8601 date
    balance         REAL,
    fetched_at      INTEGER,
    PRIMARY KEY (account_id, date)
);
"""

# v3 (schema_version 3) — the exclusions layer. Lets users permanently mark
# large one-time expenses (orthodontia, surgery, an annual insurance lump,
# a big home repair) as non-recurring so they drop out of ongoing transaction
# analysis by default. This is USER CONFIG, not cached API data: it is
# deliberately kept OUT of SCHEMA["tables"] so the skeleton clear-cache / prune
# machinery never touches it (a cache clear must not wipe the user's
# exclusions). Managed only via MonarchCache's explicit add/remove/list methods
# and manage_exclusions.py. All CREATE ... IF NOT EXISTS — idempotent.
V3_DDL = """
CREATE TABLE IF NOT EXISTS exclusions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT NOT NULL,    -- 'transaction'|'category'|'merchant'|'tag'|'category_period'
    value           TEXT NOT NULL,    -- tx id / category name / merchant name / tag name
    start_date      TEXT,             -- ISO-8601 date; NULL = all time
    end_date        TEXT,             -- ISO-8601 date; NULL = all time
    reason          TEXT,             -- user note, e.g. "one-time orthodontia"
    exclude_from    TEXT NOT NULL DEFAULT 'all',  -- 'budget'|'cashflow'|'insights'|'all'
    auto_suggested  INTEGER DEFAULT 0,  -- 1 = skill suggested; 0 = user-added
    created_at      INTEGER,
    UNIQUE(type, value, start_date, end_date)
);
CREATE INDEX IF NOT EXISTS excl_type ON exclusions(type);
"""

_V1_DDL = """
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
"""

SCHEMA = {
    "version": 3,

    # Base DDL is the union of every schema version (all IF NOT EXISTS), so a
    # fresh install gets every table in one executescript. _migrate_schema
    # handles in-place upgrades of pre-existing v1/v2 databases.
    "ddl": _V1_DDL + V2_DDL + V3_DDL,

    # ttl: prune_stale eligibility + default freshness (fetch.py passes its
    # own TTLs at call sites). Historical tables (transactions/budgets/
    # cashflow/net_worth_history/account_history) have ttl None — never
    # pruned, permanent once synced.
    # protect_where: user-corrected tax buckets survive a normal clear-cache;
    # only --force (CLI: --include-user-buckets) removes them.
    "tables": {
        "accounts": {"ttl": TTL_4H},
        "holdings": {"ttl": TTL_4H},
        "recurring": {"ttl": TTL_4H},
        "transactions": {"ttl": None},
        "budgets": {"ttl": None},
        "cashflow": {"ttl": None},
        # v2: daily snapshot tables, unlocked by MCP mode.
        "net_worth_history": {"ttl": None},
        "account_history": {"ttl": None},
        "account_tax_buckets": {"ttl": None,
                                "protect_where": "auto_classified = 0"},
    },

    # Non-reproducible user config captured by `config-export` (the durability
    # backstop). `exclusions` lives entirely outside the TTL cache; the
    # user-corrected tax-bucket rows are also exported automatically via the
    # account_tax_buckets protect_where above. All other cache data is
    # reproducible by re-syncing Monarch, so it is NOT exported.
    "config_tables": ["exclusions"],

    "clear_meta_keys": ["last_full_sync", "last_tx_sync", "tx_earliest_synced"],
}
