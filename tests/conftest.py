"""Shared pytest fixtures and path setup for the monarch-money skill tests."""
import os
import sys

# Make scripts/ importable as top-level modules (monarch_client, fetch, setup_auth).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_ROOT, "monarch-money", "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

# Test isolation: keep tests off the real persistent data dir. monarch_client
# resolves DEFAULT_DATA_DIR (the family shared dir) at import time, so point it
# at a throwaway temp dir BEFORE that import. Set both the family override and
# monarch's own env so either resolution path stays in temp.
import tempfile as _tempfile  # noqa: E402
_ISO = _tempfile.mkdtemp(prefix="mm-test-data-")
os.environ["MONARCH_MONEY_FAMILY_DIR"] = _ISO
os.environ["MONARCH_MONEY_DATA_DIR"] = _ISO

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
