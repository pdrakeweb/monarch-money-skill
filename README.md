# claude-skill-skeleton

Starting point for every Claude skill we write. Ships the shared machinery so
each skill only has to define its schema, its API sync code, and its SKILL.md
— and so improvements to the shared parts can be **pulled into every skill
repo** with a single git merge.

## What's included

| Piece | File(s) | What you get |
|-------|---------|--------------|
| Schema-driven SQLite cache | `scripts/skill_cache.py` | TTL freshness, lifetime API counters, status, prune, temp-dir writability fallback |
| Schema-driven `clear-cache` | `skill_cache.py` + `skill_schema.py` | Normal clear preserves rows matching each table's `protect_where` (user-entered data the API can't regenerate); `--force` wipes those too |
| CLI scaffold | `scripts/cli.py` | `version` / `status` / `clear-cache` built in; `--mock` fixtures, `--refresh` TTL bypass, error-JSON-exit-1 convention, UTF-8 console fix |
| Versioning | `skill_schema.py` + SKILL.md frontmatter | Dual version stamps; build refuses to package on mismatch; `cli.py version` lets the user ask Claude "what version is installed?" |
| Build | `build.ps1` / `build.sh` | Auto-discovers the skill dir; validates folder name == SKILL.md `name:` and version match; zips claude.ai-upload format with an **empty `data/`** |
| Deploy | `deploy.ps1` | Builds, auto-detects Claude Desktop's nested skills-plugin layout, backs up (keeps 5), robocopy /MIR **excluding `data\`** so sessions/cache survive redeploys, **syncs `manifest.json`** (registers the skill if missing — files alone are invisible to Claude Desktop — and keeps the description in step with SKILL.md); `-DryRun` / `-RestartClaude` / `-SkipBackup` |
| Setup validation | `validate_setup.py` | Python version, deps, data-dir writability, version-stamp match |
| Conventions | `CLAUDE.md` | Deploy-after-edit rule, semver bump rules, skeleton-update workflow, invariants |

## Starting a new skill

```powershell
git clone https://github.com/<you>/claude-skill-skeleton.git my-new-skill
cd my-new-skill
git remote rename origin skeleton                  # keep the update channel
git remote add origin <your-new-skill-repo-url>    # the skill's own repo

# Rename the skill directory IN ONE COMMIT (keeps git rename-detection happy
# so future skeleton merges follow the files):
git mv my-skill my-new-skill
git commit -m "chore: rename skill dir"
```

Then customise (per-skill files — everything else stays skeleton-shaped):
1. `my-new-skill/SKILL.md` — name (must equal the folder), version, a
   maximally-triggering description, docs.
2. `my-new-skill/scripts/skill_schema.py` — `SKILL_VERSION`, your DDL, table
   TTLs, and `protect_where` for any user-entered data.
3. `my-new-skill/scripts/cli.py` — add subcommands in the marked
   skill-specific section only.
4. `tests/` — keep `test_skeleton.py` green; add your own.

Check the wiring before writing sync code:

```powershell
python validate_setup.py
python -m pytest tests -v
.\deploy.ps1 -DryRun
```

## Pulling skeleton improvements into a skill

```powershell
git fetch skeleton
git merge skeleton/main --allow-unrelated-histories
```

Shared files merge cleanly because skills aren't supposed to edit them;
per-skill files (`SKILL.md`, `skill_schema.py`, your CLI subcommands) conflict
in your favor. If you improve a shared file while inside a skill repo, port
the change back to this skeleton so every other skill can pull it.

## Persistence model (critical — learned the hard way)

**Skill-relative `data/` does NOT survive a chat session.** Each session gets
a fresh copy of the skill (Claude Desktop local agent mode) or a fresh
container (claude.ai / mobile). The skeleton therefore resolves a persistent
data dir (`resolve_data_dir`) in this order:

1. `<SKILL>_DATA_DIR` env override
2. `/mnt/user-data/.claude-skills/<name>/` — cloud containers; persists
   across claude.ai/mobile sessions
3. `~/.claude-skills/<name>/` — desktop local agent mode; persists across
   sessions on the machine
4. skill-relative `data/` → temp dir (per-session fallbacks)

**Cross-device auth** (e.g. log in on the laptop, use it on the phone): the
only channel that reaches every device is the uploaded skill package itself.
`build.ps1 -IncludeAuth` (or `build.sh --include-auth`) bundles auth files
into `data/bootstrap/` in the zip; `bootstrap_data()` copies them into the
persistent data dir on first run of a new session, never overwriting newer
local files. Opt-in only — the secrets ship inside the package on claude.ai.
A `.env` in the persistent data dir is the zero-touch desktop pattern: each
new session reads it (e.g. credentials or a cookie string) and re-auths
without asking.

## Patterns to follow (learned the hard way)

- **Markdown summaries out, SQL in.** Scripts print compact summaries;
  Claude queries the SQLite cache for analysis. Never dump raw rows.
- **Auth options are the user's choice.** State each option's trade-off once,
  then proceed with what they picked — never refuse or stall. Never instruct
  the user to run code on their own machine; the container has Python.
- **Write APIs need a guard.** If the upstream API can mutate user data,
  wrap the client: hard-block irreversible lifecycle calls, confirm-gate the
  rest (instructions ask the user; code enforces with a one-shot arming
  context manager). See monarch-skill's `GuardedMonarch` for the reference
  implementation.
- **Validate against live shapes, not just fixtures.** GraphQL APIs nest
  labels as `{name: ...}` objects and use different vocabulary than your mocks
  (write normalisers; test them with shapes captured from the live API).
- **Secrets**: env var > `.env` > config placeholder (`load_dotenv` in
  `skill_common.py`); never print them; `data/` is git-ignored and ships
  empty in the zip.
- **Zero-as-unknown.** Upstream APIs report 0 for values they don't know
  (cost basis, etc.) — store NULL, render `—`, exclude from math.

## License

MIT
