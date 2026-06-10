"""Path setup: make the skill's scripts importable. SHARED SKELETON FILE —
auto-discovers the SKILL.md-bearing directory so renames don't break it."""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_skill_dir():
    hits = [d for d in os.listdir(_ROOT)
            if os.path.isdir(os.path.join(_ROOT, d))
            and os.path.exists(os.path.join(_ROOT, d, "SKILL.md"))]
    assert len(hits) == 1, f"expected one SKILL.md-bearing dir, found {hits}"
    return hits[0]


SKILL_DIR_NAME = _find_skill_dir()
SKILL_DIR = os.path.join(_ROOT, SKILL_DIR_NAME)
_SCRIPTS = os.path.join(SKILL_DIR, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
