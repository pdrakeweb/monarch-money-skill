"""
skill_common — shared utilities every skill CLI needs.

SHARED SKELETON FILE — improve in claude-skill-skeleton and pull into skills;
avoid skill-specific edits here (put those in skill_schema.py / your CLI).
"""
import json
import os
import sys


# --------------------------------------------------------------------------- #
# Error conventions
#
# Fatal errors print {"error": <class>, "message": <str>} as JSON on stdout
# (machine-parseable by the calling Claude) and exit 1. Subclass per skill
# for API-specific failures (auth, schema drift, network).
# --------------------------------------------------------------------------- #
class SkillError(Exception):
    """Base for fatal, non-retryable skill errors."""


class AuthExpiredError(SkillError):
    """Upstream session/credentials are dead. Re-run the skill's auth setup."""


class SchemaChangedError(SkillError):
    """Upstream API shape changed under us — upgrade the wrapper library."""


class NetworkError(SkillError):
    """Cannot reach the upstream API (DNS / timeout / connection refused)."""


def fail(exc):
    """Print the error-JSON convention and return the exit code (1)."""
    print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
    return 1


# --------------------------------------------------------------------------- #
# Console / formatting
# --------------------------------------------------------------------------- #
def force_utf8_stdout():
    """Markdown output uses ✓ — ↑ ⚠; force UTF-8 so a cp1252 Windows console
    (the skill container is Linux/UTF-8 already) doesn't raise."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def money(x):
    if x is None:
        return "—"
    if x < 0:
        return f"-${abs(x):,.2f}"
    return f"${x:,.2f}"


def age_phrase(ts):
    """Human-friendly age for 'synced X ago' headers."""
    import time
    if not ts:
        return "never"
    sec = int(time.time() - ts)
    if sec < 90:
        return "just now"
    mins = sec // 60
    if mins < 60:
        return f"{mins} min ago"
    hrs = mins // 60
    if hrs < 48:
        return f"{hrs} hr ago"
    return f"{hrs // 24} days ago"


# --------------------------------------------------------------------------- #
# Versioning
# --------------------------------------------------------------------------- #
def read_frontmatter_version(skill_md_path):
    """Read `version:` from SKILL.md frontmatter; None if absent.
    build.ps1/build.sh enforce that this matches skill_schema.SKILL_VERSION."""
    import re
    try:
        with open(skill_md_path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    m = re.search(r'^version:\s*"?([0-9]+\.[0-9]+\.[0-9]+)"?', text, re.M)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# Config helpers (.env pattern — see README "API keys & secrets")
# --------------------------------------------------------------------------- #
def load_dotenv(path):
    """Parse a .env file (KEY=value, quotes optional). Returns a dict.
    Precedence convention: real env var > .env > config.json placeholder."""
    out = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                out[k] = v
    except (OSError, UnicodeDecodeError):
        pass
    return out
