# CLAUDE.md — skill repo (from claude-skill-skeleton)

## ⛔ STOP — DEPLOYMENT REQUIRED AFTER ANY EDIT

After editing ANY file in the skill directory, run:

```powershell
.\deploy.ps1                 # build + mirror into Claude Desktop's skills dir
.\deploy.ps1 -RestartClaude  # ...and bounce Claude Desktop for you
.\deploy.ps1 -DryRun         # preview target + file changes, write nothing
```

Then **restart Claude Desktop** (unless you passed `-RestartClaude`).
The deployed copy is a separate artifact — skip this and you are testing
stale code. Deploy mirrors with robocopy /MIR **excluding `data\`**, so the
deployed cache/sessions/credentials survive every redeploy.

Claude Desktop only loads skills listed in `manifest.json` next to the
`skills\` directory — copying files alone is invisible. `deploy.ps1` syncs
the manifest: registers the skill when missing and updates its description.

### The three-location model (READ THIS before debugging "wrong version")

A custom skill can exist in three independent, non-syncing places:
1. **Account/server copy** — uploaded via claude.ai Settings → Capabilities →
   Skills. Used by **web, mobile, AND the desktop app**. Gets a server
   `skillId` like `skill_019…`.
2. **Local files** — what `deploy.ps1` writes (manifest id `skill_local_*`).
3. **The git repo** — the master; Claude never reads it directly.

**If a skill was ever uploaded via Settings, the SERVER copy is authoritative
everywhere** — local file edits and `deploy.ps1` do NOT change what loads.
There is no public API to update account skills (`/v1/skills` is workspace-
scoped for the Messages API only, invisible in the UI). To update a
server-synced skill you MUST re-upload the zip via Settings; that's also the
only way to reach web/mobile. `deploy.ps1` detects this (`Server-synced: YES`)
and prints the zip path to upload.

**Decision:**
- Want it on **phone/web** → it must be server-synced → update by re-uploading
  the zip. `deploy.ps1` still builds + validates that zip and keeps local
  files current for Claude Code / Cowork.
- **Desktop-only, fast iteration** → keep it local-only (`skill_local_*`),
  never upload it via Settings, and `deploy.ps1` + restart is the full loop.

Reporting the version: always run `cli.py version` (the live value). A session
can hold a stale SKILL.md frontmatter in context, so never trust frontmatter
for "what's installed".

## Versioning — bump on EVERY commit that touches the skill directory

The skill carries a semver version in TWO places that must always match
(build.ps1/build.sh refuse to package on mismatch; a unit test checks too):

1. `<skill>\scripts\skill_schema.py` → `SKILL_VERSION = "X.Y.Z"`
2. `<skill>\SKILL.md` frontmatter → `version: "X.Y.Z"`

Before committing any change under the skill directory:
- **patch** (X.Y.Z+1): bug fixes, doc/instruction tweaks, data-rule tweaks
- **minor** (X.Y+1.0): new commands, new auth options, new capabilities
- **major** (X+1.0.0): breaking changes — CLI flags removed/renamed, SQLite
  schema changes, auth-flow changes that require user re-setup

Update both files, commit, then tag: `git tag vX.Y.Z`. The user verifies the
installed version by asking the skill's Claude, which runs
`python scripts/cli.py version` — if that lags the repo, the deploy is stale.

## Skeleton updates — pulling shared improvements

This repo was started from claude-skill-skeleton and keeps a `skeleton`
remote. To pull improvements (cache machinery, build/deploy scripts,
versioning rules):

```powershell
git fetch skeleton
git merge skeleton/main --allow-unrelated-histories
```

Shared files (rarely edit in a skill — improve them in the skeleton instead):
`build.ps1`, `build.sh`, `deploy.ps1`, `validate_setup.py`,
`scripts/skill_cache.py`, `scripts/skill_common.py`, the shared sections of
`scripts/cli.py`, and this file's deploy/versioning sections.
Per-skill files (expected to diverge; resolve conflicts in your favor):
`SKILL.md`, `scripts/skill_schema.py`, your subcommands in `cli.py`,
`references/`, `tests/`. If a merge conflicts on a shared file, prefer the
skeleton side unless you intentionally diverged.

When you improve a SHARED file while working in a skill repo, port the change
back to claude-skill-skeleton in the same session so other skills can pull it.

## Invariants (don't break these)

- Scripts print **compact Markdown only** — never raw JSON/row dumps.
  Analysis happens by querying `data/cache.db` directly.
- **No secrets in stdout** — never print passwords, tokens, cookies, or keys.
- Fatal errors → `{"error": <class>, "message": <str>}` JSON on stdout,
  exit 1. Auth failures are fatal; never retry a dead session.
- `clear-cache` semantics are schema-driven: rows matching a table's
  `protect_where` (data the upstream API cannot regenerate) survive unless
  `--force` is passed. Declare protections in `skill_schema.py`, never
  hard-code them in CLI logic.
- The skill runs in Anthropic's Linux/UTF-8 container; `force_utf8_stdout()`
  keeps ✓/⚠/↑ glyphs from crashing a cp1252 Windows console.

## Tests

```powershell
python -m pytest tests -v
```

Quick offline check (no credentials needed):

```powershell
python <skill>\scripts\cli.py status --db $env:TEMP\demo.db
```
