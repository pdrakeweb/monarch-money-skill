"""
skill_schema — THE per-skill definition file. This is where a new skill
customises the shared machinery; everything in skill_cache.py / skill_common.py
/ cli.py should stay close to the skeleton so updates merge cleanly.

Replace the example tables with your skill's real storage.
"""

# Bump on EVERY commit that changes this skill (see CLAUDE.md for
# patch/minor/major rules). Must match the `version:` in SKILL.md
# frontmatter — build.ps1/build.sh refuse to package on mismatch.
SKILL_VERSION = "0.1.0"

# TTL constants — tune per data volatility.
TTL_4H = 4 * 3600
TTL_24H = 24 * 3600

SCHEMA = {
    # Bump when the DDL changes shape (forces attention to migrations).
    "version": 1,

    # All skill tables. A `meta` key/value table is created automatically.
    # Convention: every synced table carries a `fetched_at INTEGER` column —
    # freshness checks and prune_stale depend on it.
    "ddl": """
CREATE TABLE IF NOT EXISTS example_items (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    value       REAL,
    fetched_at  INTEGER
);

CREATE TABLE IF NOT EXISTS example_annotations (
    item_id     TEXT PRIMARY KEY,
    label       TEXT,
    user_set    INTEGER DEFAULT 0,
    fetched_at  INTEGER
);
""",

    # Per-table cache behaviour:
    #   ttl            — seconds before a normal fetch re-hits the API
    #                    (None/absent: never considered fresh / never pruned)
    #   protect_where  — SQL predicate marking rows a normal `clear-cache`
    #                    must KEEP because the upstream API cannot regenerate
    #                    them (user-entered corrections, manual labels, …).
    #                    Only `clear-cache --force` removes them.
    "tables": {
        "example_items": {"ttl": TTL_4H},
        "example_annotations": {"ttl": None, "protect_where": "user_set = 1"},
    },

    # Meta keys reset by clear-cache (sync markers). Lifetime counters
    # (api_call_count) and schema_version always survive.
    "clear_meta_keys": ["last_full_sync"],
}
