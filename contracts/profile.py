"""
profile.py — SHARED CONTRACT for the monarch-money skill family.

Single namespaced profile.json (schema 2) in the skeleton's persistent data
dir, with three sections: identity / finance / retirement. Each skill reads the
WHOLE profile and writes ONLY its own section(s); `identity` is writable by all
(last-write-wins). A flat v0.8/0.9 profile is migrated to schema 2 on first
load, preserving every value.

This file is the canonical copy. Vendor it verbatim into each skill's
`scripts/` (do not edit per-skill — improve here and re-copy). See CONTRACTS.md.

Consumers typically:
    prof = load_profile(data_dir)          # namespaced dict (migrated in place)
    flat = flat_view(prof)                  # merged flat dict for report reads
    save_section(data_dir, "finance", {...})  # write back only your section
"""
import json
import os

PROFILE_SCHEMA = 2

# Flat (v0.8/0.9) key → owning section. Anything not listed defaults to finance.
_IDENTITY_KEYS = {"birth_year", "state", "filing_status"}
_RETIREMENT_KEYS = {
    "retirement_age", "life_expectancy_age", "withdrawal_strategy", "swr_pct",
    "spending_tilt_pct", "spending_tilt_schedule", "expected_real_return_pct",
    "equity_vol_pct", "annual_new_savings", "income_streams", "social_security",
    "retirement_expense_deltas", "retirement_trad_tax_rate",
    "retirement_ltcg_rate", "relocate_no_income_tax_state", "confidence_band",
    "guardrail_initial_swr_pct", "guardrail_band_pct", "income_discount_rate_pct",
    "retirement_expense_reductions",
    # pre-80 mortgage-aware lean-floor risk gauge (TPAW Monte Carlo)
    "lean_floor_pre_payoff", "lean_floor_post_payoff", "lean_floor_payoff_age",
    "lean_window_max_age",
    # category-level retirement budget transforms (age-indexed)
    "retirement_category_transforms",
}
# Top-level (not a section) markers preserved across migration.
_TOPLEVEL_KEYS = {"profile_complete"}


def _section_for(key):
    if key in _IDENTITY_KEYS:
        return "identity"
    if key in _RETIREMENT_KEYS:
        return "retirement"
    return "finance"


def empty_profile():
    return {"schema": PROFILE_SCHEMA, "profile_complete": False,
            "identity": {}, "finance": {}, "retirement": {}}


def migrate_flat_to_v2(flat):
    """Route a flat v0.8/0.9 profile into the namespaced schema-2 shape,
    preserving every value. Idempotent: an already-namespaced dict is returned
    with its sections intact (missing sections filled)."""
    if isinstance(flat, dict) and flat.get("schema") == PROFILE_SCHEMA:
        out = empty_profile()
        out["profile_complete"] = flat.get("profile_complete", False)
        for sec in ("identity", "finance", "retirement"):
            out[sec] = dict(flat.get(sec) or {})
        return out
    out = empty_profile()
    for key, val in (flat or {}).items():
        if key in ("schema",):
            continue
        if key in _TOPLEVEL_KEYS:
            out[key] = val
            continue
        out[_section_for(key)][key] = val
    return out


def _path(data_dir):
    return os.path.join(data_dir, "profile.json")


def load_profile(data_dir, persist=True):
    """Return the namespaced profile (schema 2). If profile.json is flat/older,
    migrate it and (by default) persist the migration in place."""
    path = _path(data_dir)
    if not os.path.exists(path):
        return empty_profile()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return empty_profile()
    migrated = migrate_flat_to_v2(raw)
    needed = raw.get("schema") != PROFILE_SCHEMA
    if needed and persist:
        try:
            _atomic_write(path, migrated)
        except OSError:
            pass
    return migrated


def save_section(data_dir, section, values, mark_complete=None):
    """Merge `values` into one section (identity|finance|retirement) and write.
    Reads-modifies-writes the whole file so sibling sections are preserved."""
    if section not in ("identity", "finance", "retirement"):
        raise ValueError(f"unknown profile section: {section}")
    prof = load_profile(data_dir, persist=False)
    prof.setdefault(section, {}).update(values or {})
    if mark_complete is not None:
        prof["profile_complete"] = bool(mark_complete)
    os.makedirs(data_dir, exist_ok=True)
    _atomic_write(_path(data_dir), prof)
    return prof


def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, path)


def flat_view(profile):
    """Merge identity + finance + retirement into one flat dict for report code
    that reads `profile["swr_pct"]` etc. Later sections win on key collisions
    (none expected — keys are partitioned). `profile_complete` is included."""
    out = {}
    for sec in ("identity", "finance", "retirement"):
        out.update(profile.get(sec) or {})
    out["profile_complete"] = profile.get("profile_complete", False)
    out["schema"] = profile.get("schema", PROFILE_SCHEMA)
    return out
