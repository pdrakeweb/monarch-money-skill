"""
secret_provider.py — SHARED CONTRACT: read secrets from the secrets vault.

Vendor this file VERBATIM into any skill that needs secrets (like cache_contract
.py / profile.py). It is **read-only**: it returns a secret by name from the
shared vault store, or None when the vault isn't installed/published or the
secret is absent. It NEVER prints values — callers use the returned value
in-process (e.g. to authenticate) and must not echo it.

The vault skill (`secrets`) owns WRITING the store; consumers only read.
Both resolve the same shared location so a consumer finds what the vault wrote:
    $CLAUDE_SECRETS_DIR  >  /mnt/user-data/.claude-skills/_secrets/  (cloud)
                         >  ~/.claude-skills/_secrets/               (desktop)

Usage in a consumer:
    from secret_provider import get_secret
    cookies = get_secret("monarch_cookies")     # None if vault not available
    if cookies: ...authenticate with it (never print it)...
"""
import json
import os

STORE_FILENAME = "secrets.json"


def _candidate_dirs():
    # $CLAUDE_SECRETS_DIR is AUTHORITATIVE — when set it is the only location
    # (so an explicit override fully isolates; nothing else is consulted).
    override = os.environ.get("CLAUDE_SECRETS_DIR")
    if override:
        return [override]
    out = []
    if os.path.isdir("/mnt/user-data"):
        out.append(os.path.join("/mnt", "user-data", ".claude-skills", "_secrets"))
    home = os.path.expanduser("~")
    if home and home != "~":
        out.append(os.path.join(home, ".claude-skills", "_secrets"))
    out.append(os.path.join(os.getcwd(), ".secrets"))
    return out


def store_dir():
    """The shared vault dir. Prefer one that already has a store file; else the
    most-preferred candidate (what a writer would create)."""
    cands = _candidate_dirs()
    for d in cands:
        if os.path.exists(os.path.join(d, STORE_FILENAME)):
            return d
    return cands[0]


def store_path():
    return os.path.join(store_dir(), STORE_FILENAME)


def read_store():
    """Return the parsed store dict, or {} if absent/unreadable."""
    try:
        with open(store_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def vault_available():
    """True iff a published vault store exists (does not validate contents)."""
    return os.path.exists(store_path())


def get_secret(name, default=None):
    """Return the secret VALUE for `name`, or `default`. Never prints."""
    sec = (read_store().get("secrets") or {}).get(name)
    if isinstance(sec, dict):
        return sec.get("value", default)
    return sec if sec is not None else default


def secret_names():
    """Names only (safe to display) — never the values."""
    return sorted((read_store().get("secrets") or {}).keys())
