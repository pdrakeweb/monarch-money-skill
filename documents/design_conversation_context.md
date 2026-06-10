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

monarchmoneycommunity==1.3.2 (bradleyseanf fork). Pins gql==4.0 — do not
override. API is async throughout; use asyncio.run() wrappers.
