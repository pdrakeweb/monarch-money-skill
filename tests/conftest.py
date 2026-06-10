"""Shared pytest fixtures and path setup for the monarch-money skill tests."""
import os
import sys

# Make scripts/ importable as top-level modules (monarch_client, fetch, setup_auth).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_ROOT, "monarch-money", "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
