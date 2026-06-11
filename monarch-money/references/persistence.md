# Where auth & cache live — and why credentials sometimes vanish

## The short version

- **Auth/cache are stored in a persistent data directory, NOT inside the skill
  package.** Get the exact path + a persistence verdict from
  `python scripts/fetch.py auth-status`.
- **Desktop (local agent mode):** the dir is `~/.claude-skills/monarch-money/`
  on your real machine. It **survives skill updates AND new chats.** Set up
  auth once; later chats reuse it.
- **Cloud / mobile (claude.ai web, phone):** the container's persistent dir
  (`/mnt/user-data/...`) persists **within one conversation only**. A brand-new
  chat gets a fresh container — so filesystem auth does NOT carry across chats
  there. This is a platform property, not a skill bug.

## Why credentials were lost after an update (the bug you hit)

Skill versions **before v0.6.0** saved auth into the skill's own `data/`
folder — which lives *inside the skill package*. Re-uploading/replacing the
skill swaps that package out, taking `data/session.pickle` (and any
credentials) with it. So "update the skill → lose credentials" was expected
for the old layout. v0.6.0+ stores auth in the persistent data dir instead, so
**updating the skill no longer wipes auth.**

## Making auth persist — by environment

### Desktop — just set up once
Run any auth option once (Option A/B/C/D in SKILL.md §2). It lands in
`~/.claude-skills/monarch-money/` and persists across chats and updates.
`auth-status` will show `Persistence: home`.

### Cloud / mobile / cross-device (laptop → phone) — bundle a `.env`
Because each new cloud chat starts fresh, the only thing guaranteed present in
every session on every device is **the uploaded skill package itself**. Put
durable credentials in a `.env` and bundle it into the package:

1. Create `~/.claude-skills/monarch-money/.env` (or `<skill>/data/.env`) with
   EITHER cookies or login creds:
   ```
   MONARCH_COOKIES=session_id=...; csrftoken=...
   # — or —
   MONARCH_EMAIL=you@example.com
   MONARCH_PASSWORD=your-password
   MONARCH_MFA_SECRET=BASE32TOTPSECRET
   ```
2. Build with auth bundled and re-upload:
   ```
   .\build.ps1 -IncludeAuth      # or: bash build.sh --include-auth
   ```
   Then Settings → Capabilities → Skills → re-upload `monarch-money.zip`.
3. Every new chat on every device runs `bootstrap_data()` on first use, copying
   the bundled `.env` into its (fresh) data dir; `ensure_authenticated()` then
   re-auths from it with zero prompts.

**Trade-off:** the secrets ride *inside* the uploaded package (stored on
claude.ai). Prefer `MONARCH_COOKIES` (revocable by logging out of Monarch) or
credentials you're willing to rotate. This is opt-in for exactly this reason.

Login-based `.env` (email/password/MFA secret) is the most robust for
cross-device because it **re-logs-in fresh** each session — it doesn't depend
on a saved session token surviving. Cookie `.env` works until the browser
session expires.

## Quick diagnosis

`python scripts/fetch.py auth-status` reports the data dir, a persistence
verdict (`home` ✓ / `cloud` • / `skill`|`temp` ⚠), and which auth files are
present. If it says `Persistence: skill` or `temp`, auth there will not
survive — move to the home dir (desktop) or bundle a `.env` (cloud/mobile).
