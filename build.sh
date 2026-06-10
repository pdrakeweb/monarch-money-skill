#!/usr/bin/env bash
# SHARED SKELETON FILE — POSIX counterpart of build.ps1. Auto-discovers the
# skill dir; validates name + version; zips with empty data/.
# --include-auth: bundle auth files (session.pickle/credentials.json/
# cookie_session.json/.env) into data/bootstrap/ for cross-device bootstrap.
# The secrets ship inside the uploaded package — opt-in only.
set -euo pipefail
cd "$(dirname "$0")"
INCLUDE_AUTH=0
[ "${1:-}" = "--include-auth" ] && INCLUDE_AUTH=1

# --- discover the skill directory ---------------------------------------------
skill_dirs=()
for d in */; do
    [ -f "$d/SKILL.md" ] && skill_dirs+=("${d%/}")
done
if [ "${#skill_dirs[@]}" -ne 1 ]; then
    echo "ERROR: expected exactly one SKILL.md-bearing directory; found ${#skill_dirs[@]}" >&2
    exit 1
fi
SKILL_DIR="${skill_dirs[0]}"
OUT_NAME="$SKILL_DIR.zip"

# --- validate folder name matches frontmatter ----------------------------------
skill_name=$(sed -n 's/^name:[[:space:]]*"\{0,1\}\([^"[:space:]]*\)"\{0,1\}.*/\1/p' "$SKILL_DIR/SKILL.md" | head -1)
if [ "$skill_name" != "$SKILL_DIR" ]; then
    echo "ERROR: SKILL.md name '$skill_name' does not match folder '$SKILL_DIR'" >&2
    exit 1
fi

# --- validate frontmatter version matches SKILL_VERSION ------------------------
md_ver=$(sed -n 's/^version:[[:space:]]*"\{0,1\}\([0-9][0-9.]*\)"\{0,1\}.*/\1/p' "$SKILL_DIR/SKILL.md" | head -1)
py_ver=$(sed -n 's/^SKILL_VERSION[[:space:]]*=[[:space:]]*"\([0-9][0-9.]*\)".*/\1/p' "$SKILL_DIR"/scripts/*.py | head -1)
if [ -z "$md_ver" ] || [ -z "$py_ver" ]; then
    echo "ERROR: missing version: in SKILL.md frontmatter or SKILL_VERSION in scripts/*.py" >&2
    exit 1
fi
if [ "$md_ver" != "$py_ver" ]; then
    echo "ERROR: version mismatch: SKILL.md says $md_ver, scripts say $py_ver. Bump both (see CLAUDE.md)." >&2
    exit 1
fi

rm -f "$OUT_NAME"

# --- stage with empty data/ -----------------------------------------------------
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cp -R "$SKILL_DIR" "$tmp/$SKILL_DIR"
find "$tmp/$SKILL_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} +
rm -rf "$tmp/$SKILL_DIR/data"
mkdir -p "$tmp/$SKILL_DIR/data"
echo "This directory is created at runtime (cache, sessions). Ships empty." \
    > "$tmp/$SKILL_DIR/data/.gitkeep"

if [ "$INCLUDE_AUTH" = "1" ]; then
    bootstrap="$tmp/$SKILL_DIR/data/bootstrap"
    mkdir -p "$bootstrap"
    bundled=""
    for f in session.pickle credentials.json cookie_session.json .env; do
        for src in "$HOME/.claude-skills/$SKILL_DIR" "$PWD/$SKILL_DIR/data"; do
            if [ -f "$src/$f" ]; then cp "$src/$f" "$bootstrap/"; bundled="$bundled $f"; break; fi
        done
    done
    if [ -z "$bundled" ]; then
        echo "ERROR: --include-auth set but no auth files found" >&2
        exit 1
    fi
    echo "WARNING: AUTH BUNDLED into the zip:$bundled (ships inside the uploaded package)" >&2
fi

out_abs="$PWD/$OUT_NAME"
if command -v zip >/dev/null 2>&1; then
    (cd "$tmp" && zip -qr "$out_abs" "$SKILL_DIR")
else
    # No zip binary — use Python. Probe candidates with --version: on Windows
    # a bare `python` may be the Microsoft Store stub.
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
echo "Built: $OUT_NAME v$md_ver ($size bytes)"
