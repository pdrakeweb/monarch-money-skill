---
name: "my-skill"
version: "0.1.0"
description: >
  REPLACE ME — maximally-triggering description of what data/capability this
  skill provides. Enumerate the user phrasings that should trigger it, and
  end with "even if they don't mention <product> by name."
---

# My Skill

REPLACE ME — one paragraph: what the skill does, where the data lives
(`data/cache.db`), and the golden rule: scripts print compact Markdown
summaries; for real analysis, query the SQLite cache directly with SQL/pandas.

All commands run from the skill directory: `python scripts/cli.py <command>`.

## Command reference

| Command | Description | Flags |
|---------|-------------|-------|
| `version` | Print the installed skill version | — |
| `status` | Cache row counts, ages, lifetime API calls | — |
| `clear-cache` | Empty the cache; protected rows survive | `--force` |

If the user asks **which version of the skill is installed**, you MUST run
`python scripts/cli.py version` and report THAT output. Do not answer from
this file's frontmatter or from memory — a stale session can carry an old
frontmatter in context, so only the live command is authoritative. If the
reported version is older than expected, the skill is loading a stale copy
(see "How updates reach this skill" below).

## How updates reach this skill

A custom skill exists in up to three independent places (they do NOT
auto-sync): (1) the account/server copy used by claude.ai web, mobile, AND
the desktop app; (2) local files on each machine; (3) the source repo. If
this skill was uploaded via **Settings → Capabilities → Skills**, the
**server copy is what loads everywhere** — editing local files or re-running
deploy will NOT change the running version. To update a server-synced skill
you must re-upload the zip via Settings (there is no API for it). `deploy.ps1`
prints whether the skill is server-synced and the exact zip to upload.

`clear-cache` keeps rows the upstream API cannot regenerate (user corrections
and the like, per `skill_schema.py` `protect_where`); only `--force` removes
those. Offer `clear-cache` whenever a full re-pull / re-inference is wanted.

## REPLACE ME — sections every finished skill needs

1. Quick-start decision tree (auth valid? → sync? → query SQLite?)
2. First-run setup / auth, with trade-offs stated once — then follow the
   user's choice; never refuse an option they picked
3. Worked examples (command + the SQL Claude runs afterwards)
4. Honest limitations
5. Error handling (the error-JSON convention: `{"error", "message"}`, exit 1)
