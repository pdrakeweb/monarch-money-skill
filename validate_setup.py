#!/usr/bin/env python3
"""
validate_setup.py — environment sanity check for this skill.

SHARED SKELETON FILE. Verifies:
  1. Python version (3.10+)
  2. requirements.txt packages import
  3. The skill directory is discoverable and name matches frontmatter
  4. Version stamps match (SKILL.md frontmatter vs skill_schema.SKILL_VERSION)
  5. data/ is writable (or the cache falls back to temp)
  6. The CLI smoke-runs (version + status against a temp DB)
"""
import importlib
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
PASS, FAIL, WARN = "  OK:", "  FAIL:", "  WARN:"


def find_skill_dir():
    hits = [d for d in os.listdir(REPO)
            if os.path.isdir(os.path.join(REPO, d))
            and os.path.exists(os.path.join(REPO, d, "SKILL.md"))]
    return hits[0] if len(hits) == 1 else None


def main():
    failures = 0

    print("[1/6] Python version...")
    if sys.version_info >= (3, 10):
        print(PASS, sys.version.split()[0])
    else:
        print(FAIL, f"need 3.10+, have {sys.version.split()[0]}")
        failures += 1

    print("[2/6] requirements.txt imports...")
    req = os.path.join(REPO, "requirements.txt")
    if os.path.exists(req):
        with open(req, encoding="utf-8") as fh:
            for line in fh:
                pkg = re.split(r"[=<>\[;]", line.strip())[0]
                if not pkg or pkg.startswith("#"):
                    continue
                mod = pkg.replace("-", "_")
                try:
                    importlib.import_module(mod)
                    print(PASS, pkg)
                except ImportError:
                    print(FAIL, f"{pkg} not importable — pip install -r requirements.txt")
                    failures += 1
    else:
        print(WARN, "no requirements.txt")

    print("[3/6] Skill directory...")
    skill = find_skill_dir()
    if not skill:
        print(FAIL, "expected exactly one SKILL.md-bearing directory")
        return 1
    with open(os.path.join(REPO, skill, "SKILL.md"), encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r'^name:\s*"?([^"\s]+)"?', text, re.M)
    if m and m.group(1) == skill:
        print(PASS, f"{skill} (name matches frontmatter)")
    else:
        print(FAIL, f"folder '{skill}' vs frontmatter name '{m.group(1) if m else '?'}'")
        failures += 1

    print("[4/6] Version stamps...")
    mv = re.search(r'^version:\s*"?([0-9]+\.[0-9]+\.[0-9]+)"?', text, re.M)
    sys.path.insert(0, os.path.join(REPO, skill, "scripts"))
    try:
        import skill_schema
        pv = skill_schema.SKILL_VERSION
    except Exception as exc:  # noqa: BLE001
        print(FAIL, f"cannot import skill_schema: {exc}")
        return 1
    if mv and mv.group(1) == pv:
        print(PASS, f"v{pv}")
    else:
        print(FAIL, f"SKILL.md={mv.group(1) if mv else '?'} vs skill_schema={pv}")
        failures += 1

    print("[5/6] data/ writability...")
    data_dir = os.path.join(REPO, skill, "data")
    try:
        os.makedirs(data_dir, exist_ok=True)
        probe = os.path.join(data_dir, ".write_probe")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        print(PASS, data_dir)
    except OSError as exc:
        print(WARN, f"{exc} — cache will fall back to {tempfile.gettempdir()}")

    print("[6/6] CLI smoke test...")
    cli = os.path.join(REPO, skill, "scripts", "cli.py")
    tmp_db = os.path.join(tempfile.gettempdir(), "skill_validate.db")
    for cmd in ("version", "status"):
        r = subprocess.run([sys.executable, cli, cmd, "--db", tmp_db],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            print(PASS, f"cli.py {cmd}")
        else:
            print(FAIL, f"cli.py {cmd} -> exit {r.returncode}: {r.stderr.strip()[:200]}")
            failures += 1

    print()
    if failures:
        print(f"{failures} check(s) failed.")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
