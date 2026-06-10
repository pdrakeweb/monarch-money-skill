# Design Decision Context

## Skill vs MCP Server

Decision: Custom claude.ai skill (not local MCP server).
Reason: Skills execute in Anthropic's container, accessible from mobile and
desktop. Local MCP servers require a running host — desktop only. The
forecast-skill precedent confirmed session + SQLite persistence works in the
container environment.

## Auth

Option A default (session only, no stored password) — smallest credential
footprint, revocable at Monarch's security settings.
Option B (persist-credentials) — complete credentials stored, zero-touch
reauth. Explicit opt-in only; SKILL.md must confirm with user first.
Option C (token, added in review) — user copies the session token from the
Monarch web app's DevTools and shares only that; no password/MFA secret ever
transits the conversation. Recommended for users without local Python, since
auth setup then happens inside the claude.ai container via chat.

## No-local-Python flow

The skill container has Python, so setup_auth.py runs in-conversation. Claude
must state trade-offs before the user sends secrets: A/B put the password in
chat history (suggest rotation after); TOTP codes expire in ~30s so the login
must run immediately; C avoids both.

## Write policy (decided 2026-06)

Not read-only: a write-capable client with a two-tier guard. Account lifecycle
(create_manual_account, delete_account) is forbidden outright — blocked by
instructions AND by the GuardedMonarch proxy, no override. Every other write
API requires Claude to verify the specific action with the user in chat, and
the proxy independently requires per-method arming via
`with client.mm.allow("<method>"):` (one-shot, with-block scoped). Double
layer on purpose: instructions create the confirmation UX, code makes skipping
it impossible.

Option C clarification: bearer tokens are still mintable via the login API
(short script reading mm.token after login()) — only browser extraction died.

Field lessons (2026-06, second round):
- The Monarch web app no longer exposes a bearer token to the browser — auth
  is cookie-only (csrftoken + sessionid). Token extraction instructions were
  removed; Option D (cookies) is the browser-based no-password route, and
  Option C is reserved for tokens obtained elsewhere.
- Rotating the Monarch password does NOT invalidate a saved session. Docs now
  recommend "authenticate once, then rotate" as the standard mitigation for
  pasting a password into chat (Option A).

Field lesson (2026-06): an early SKILL.md draft labeled Option C "recommended"
with heavy caveats on A — the in-session Claude over-applied that and REFUSED
the user's own email/password, then improvised local-Python token extraction
(worse: secrets land in shell history, and the user may have no Python).
SKILL.md now states explicitly: all options are legitimate, state the trade-off
once and proceed with the user's choice, never refuse Option A, and never
instruct the user to run code on their own machine.

## Output Format

Compact Markdown summaries to stdout. Claude queries SQLite for analysis.
Claude generates Markdown/HTML artifacts in-conversation. No HTML from scripts.

## Tax Bucket Classification

Auto-classify from Monarch subtype on every sync. User can correct by setting
auto_classified=0; those rows are never overwritten. SKILL.md documents the
SQL correction pattern.

## Lot-Level Limitation

Monarch provides position-level holdings only. No per-lot basis or acquisition
dates. TLH scans show which tickers have losses; precise amounts and wash-sale
analysis require brokerage CSV supplementation. SKILL.md must state this plainly.

## Core Library

monarchmoneycommunity==1.4.0 (bradleyseanf fork). Pins gql==4.0 — do not
override. API is async throughout; use asyncio.run() wrappers. 1.4.0 added
native cookie auth (login_with_cookies / set_cookies, REQUIRED_COOKIES =
session_id + csrftoken) and the session pickle persists cookie sessions.

## QA findings (2026-06, live 55-account household)

Comprehensive read/write API test under cookie auth: 22/23 reads pass, 8/8
reversible writes pass — cookie auth fully equivalent to token auth. Bugs
fixed from that run: live type vocabulary is 'brokerage' not 'investment'
(holdings sync + tax warnings); budget category names live under
categoryGroups[].categories[], not on the category nodes; net-worth sublines
needed depository/brokerage/real_estate labels; basis=0 means unknown (store
NULL, exclude from TLH; null-ticker employer funds never get TLH swaps).
Library quirk: get_aggregate_snapshots needs ISO date strings, not date
objects. API gap: no delete_transaction_tag — tags are permanent via API.
