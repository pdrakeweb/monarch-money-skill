#!/usr/bin/env bash
# Package the monarch-money skill into monarch-money.zip.
# POSIX counterpart of build.ps1: validates the SKILL.md frontmatter name
# matches the folder, stages a copy with an EMPTY data/ dir, and zips it
# with a top-level monarch-money/ folder (claude.ai upload format).
set -euo pipefail

cd "$(dirname "$0")"
SKILL_DIR="monarch-money"
OUT_NAME="monarch-money.zip"

# --- validate folder name matches SKILL.md frontmatter -----------------------
skill_name=$(sed -n 's/^name:[[:space:]]*"\{0,1\}\([^"[:space:]]*\)"\{0,1\}.*/\1/p' "$SKILL_DIR/SKILL.md" | head -1)
if [ -z "$skill_name" ]; then
    echo "ERROR: could not read 'name:' from $SKILL_DIR/SKILL.md" >&2
    exit 1
fi
if [ "$skill_name" != "$SKILL_DIR" ]; then
    echo "ERROR: SKILL.md name '$skill_name' does not match folder '$SKILL_DIR'" >&2
    exit 1
fi

rm -f "$OUT_NAME"

# --- stage to temp so we can guarantee data/ ships empty ----------------------
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cp -R "$SKILL_DIR" "$tmp/$SKILL_DIR"

find "$tmp/$SKILL_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} +
rm -rf "$tmp/$SKILL_DIR/data"
mkdir -p "$tmp/$SKILL_DIR/data"
echo "This directory is created at runtime to hold the SQLite cache and session." \
    > "$tmp/$SKILL_DIR/data/.gitkeep"

out_abs="$PWD/$OUT_NAME"
if command -v zip >/dev/null 2>&1; then
    (cd "$tmp" && zip -qr "$out_abs" "$SKILL_DIR")
else
    # No zip binary (common on minimal/Git-Bash environments) — use Python.
    # Probe candidates with --version: on Windows a bare `python` may be the
    # Microsoft Store stub, which exists on PATH but cannot run anything.
    py=""
    for cand in python3 python "py -3"; do
        if $cand --version >/dev/null 2>&1; then py="$cand"; break; fi
    done
    if [ -z "$py" ]; then
        echo "ERROR: need either 'zip' or a working Python to build." >&2
        exit 1
    fi
    (cd "$tmp" && $py - "$out_abs" "$SKILL_DIR" <<'EOF'
import os, sys, zipfile
out, root = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
    for dirpath, _, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            zf.write(p, p.replace(os.sep, "/"))
EOF
    )
fi

size=$(wc -c < "$OUT_NAME" | tr -d ' ')
echo "Built: $OUT_NAME ($size bytes)"
