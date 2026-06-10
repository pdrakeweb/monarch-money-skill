"""
setup_auth.py — one-time Monarch Money login for the monarch-money skill.

Option A (default):
    python scripts/setup_auth.py --email E --password P --mfa-code 123456
  Logs in, saves ONLY the session pickle. The password is never written to disk.
  Re-run with a fresh TOTP code when the session expires.

Option B (opt-in, --persist-credentials):
    python scripts/setup_auth.py --email E --password P \
        --mfa-secret-key BASE32SECRET --persist-credentials
  Additionally writes data/credentials.json (email + password + mfa_secret_key)
  so the skill can re-authenticate with zero touch. SKILL.md requires explicit
  user confirmation before this path is used.

Option C (token — no password ever shared):
    python scripts/setup_auth.py --token MONARCH_SESSION_TOKEN
  Takes an existing session token copied from the Monarch web app (browser
  DevTools), validates it with a lightweight API call, and saves the session
  pickle. No email, password, or MFA material is involved. This is the
  recommended path when auth is set up inside the claude.ai container.

Option D (browser cookies — no password ever shared):
    python scripts/setup_auth.py --csrf-token CSRFTOKEN --session-id SESSION_ID
    python scripts/setup_auth.py --cookie "session_id=...; csrftoken=..."
  Takes the `csrftoken` and `session_id` cookie values from a logged-in
  app.monarch.com browser session (DevTools -> Application -> Cookies),
  validates them via login_with_cookies, and saves the standard session
  pickle (library >= 1.4 persists cookie sessions natively). Easiest
  extraction: the Cookies panel shows both values directly, including the
  HttpOnly session_id that JS cannot read.

Nothing in this module ever prints the password, session token, MFA secret,
or cookie values.
"""
import argparse
import asyncio
import inspect
import json
import os
import sys

# Allow running as a standalone script (scripts/ on sys.path).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from monarch_client import (  # noqa: E402
    classify_error, apply_cookie_auth, MonarchSkillError,
    DEFAULT_SESSION_PATH, DEFAULT_CRED_PATH, DEFAULT_COOKIE_PATH,
    DEFAULT_DATA_DIR,
)


def _maybe_run(result):
    """Await a coroutine if we got one; otherwise return as-is.

    The real MonarchMoney methods are async (return coroutines). In tests the
    client is a MagicMock whose methods return plain mocks — not awaitable — so
    we must not blindly call asyncio.run().
    """
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def perform_login(mm, email, password, mfa_code=None, mfa_secret_key=None):
    """Drive the login handshake, handling the MFA challenge.

    Tries a normal login first. If Monarch demands MFA and we have a one-time
    code, completes via multi_factor_authenticate. If we only have a TOTP
    secret, the underlying library handles it inside login(mfa_secret_key=...).
    """
    from monarchmoney import RequireMFAException
    try:
        _maybe_run(mm.login(
            email, password,
            use_saved_session=False,
            save_session=False,
            mfa_secret_key=mfa_secret_key,
        ))
    except RequireMFAException:
        if not mfa_code:
            raise
        _maybe_run(mm.multi_factor_authenticate(email, password, mfa_code))


def write_credentials(cred_path, email, password, mfa_secret_key=None):
    os.makedirs(os.path.dirname(os.path.abspath(cred_path)), exist_ok=True)
    payload = {"email": email, "password": password}
    if mfa_secret_key:
        payload["mfa_secret_key"] = mfa_secret_key
    with open(cred_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    try:
        os.chmod(cred_path, 0o600)
    except OSError:
        pass  # best-effort on platforms without POSIX perms


def parse_cookie_string(cookie_string):
    """Parse 'k=v; k2=v2' into a dict. Accepts the legacy 'sessionid' name
    and canonicalises it to Monarch's 'session_id'."""
    out = {}
    for part in (cookie_string or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    if "sessionid" in out and "session_id" not in out:
        out["session_id"] = out.pop("sessionid")
    return out


def run_setup(email=None, password=None, mfa_code=None, persist=False,
              session_path=DEFAULT_SESSION_PATH, cred_path=DEFAULT_CRED_PATH,
              cookie_path=DEFAULT_COOKIE_PATH, mfa_secret_key=None, token=None,
              csrf_token=None, session_id=None, cookie_string=None, mm=None):
    """Perform login (or adopt a token / browser cookies), save the session,
    optionally persist credentials. Returns a small status dict. Never returns
    or prints secret material.
    """
    try:
        os.makedirs(os.path.dirname(os.path.abspath(session_path)), exist_ok=True)
    except PermissionError:
        raise MonarchSkillError(
            f"Cannot create the session directory at {os.path.dirname(session_path)} "
            "(read-only filesystem). Pass --session-path to a writable location."
        )

    if mm is None:
        from monarchmoney import MonarchMoney
        mm = MonarchMoney(session_file=session_path)

    if persist and (token or csrf_token or cookie_string):
        raise MonarchSkillError(
            "--persist-credentials requires --email/--password; it cannot be "
            "combined with --token or cookies (those are expiring sessions, "
            "not reusable credentials)."
        )

    if cookie_string and not (csrf_token and session_id):
        cookies = parse_cookie_string(cookie_string)
        csrf_token = cookies.get("csrftoken")
        session_id = cookies.get("session_id")
        if not (csrf_token and session_id):
            raise MonarchSkillError(
                "--cookie must contain both session_id and csrftoken, e.g. "
                '--cookie "session_id=...; csrftoken=..."'
            )

    if csrf_token and session_id:
        # Option D: adopt the browser's session cookies (Monarch names them
        # session_id and csrftoken). login_with_cookies (library >= 1.4)
        # validates them with a real API call; the standard session pickle
        # persists auth_mode='cookie' + the cookie dict, so from_session
        # reloads cookie auth natively on every later fetch.
        canonical = f"session_id={session_id}; csrftoken={csrf_token}"
        if hasattr(mm, "login_with_cookies"):
            _maybe_run(mm.login_with_cookies(
                canonical, save_session=False, verify=True))
        else:  # pre-1.4 fallback: manual headers + validation ping
            apply_cookie_auth(mm, csrf_token, session_id)
            _maybe_run(mm.get_subscription_details())
        mm.save_session(session_path)
        return {"session_path": session_path,
                "persisted_credentials": False, "method": "cookies"}

    if token:
        # Option C: adopt an existing session token. Validate it with one
        # lightweight read before saving so a bad paste fails loudly here
        # rather than on the first real query.
        mm.set_token(token)
        _maybe_run(mm.get_subscription_details())
    else:
        perform_login(mm, email, password, mfa_code=mfa_code,
                      mfa_secret_key=mfa_secret_key)
    mm.save_session(session_path)

    result = {"session_path": session_path, "persisted_credentials": False,
              "method": "token" if token else "login"}
    if persist:
        write_credentials(cred_path, email, password, mfa_secret_key)
        result["persisted_credentials"] = True
        result["cred_path"] = cred_path
    return result


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(
        description="One-time Monarch Money login (saves a session pickle).")
    p.add_argument("--email")
    p.add_argument("--password")
    p.add_argument("--mfa-code", help="One-time TOTP code for this login (Option A).")
    p.add_argument("--mfa-secret-key",
                   help="Base32 TOTP secret (enables zero-touch reauth with -B).")
    p.add_argument("--token",
                   help="Option C: existing Monarch session token (from the web "
                        "app's DevTools). No email/password needed.")
    p.add_argument("--csrf-token",
                   help="Option D: csrftoken cookie value from a logged-in "
                        "app.monarch.com browser session. Use with "
                        "--session-id.")
    p.add_argument("--session-id",
                   help="Option D: session_id cookie value (DevTools -> "
                        "Application -> Cookies). Use with --csrf-token.")
    p.add_argument("--cookie",
                   help='Option D alternative: whole cookie string, e.g. '
                        '"session_id=...; csrftoken=..."')
    p.add_argument("--persist-credentials", action="store_true",
                   help="Option B: also store email+password+mfa_secret in "
                        "data/credentials.json. Requires user confirmation.")
    p.add_argument("--session-path", default=DEFAULT_SESSION_PATH)
    p.add_argument("--cred-path", default=DEFAULT_CRED_PATH)
    p.add_argument("--cookie-path", default=DEFAULT_COOKIE_PATH)
    args = p.parse_args(argv)

    if bool(args.csrf_token) != bool(args.session_id):
        p.error("--csrf-token and --session-id must be provided together")
    if not args.token and not args.csrf_token and not args.cookie \
            and not (args.email and args.password):
        p.error("provide --token, --cookie, --csrf-token with --session-id, "
                "or both --email and --password")

    try:
        result = run_setup(
            email=args.email,
            password=args.password,
            mfa_code=args.mfa_code,
            persist=args.persist_credentials,
            session_path=args.session_path,
            cred_path=args.cred_path,
            cookie_path=args.cookie_path,
            mfa_secret_key=args.mfa_secret_key,
            token=args.token,
            csrf_token=args.csrf_token,
            session_id=args.session_id,
            cookie_string=args.cookie,
        )
    except MonarchSkillError as exc:
        print(f"Auth setup failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        classified = classify_error(exc)
        print(f"Auth setup failed: {classified}", file=sys.stderr)
        return 1

    # Success message — deliberately mentions no secret material.
    if result.get("method") == "cookies":
        print("✓ Monarch cookie session saved to:", result["session_path"])
        print("  Authenticated via browser cookies (Option D).")
        print("  When the browser session expires, copy fresh cookie values "
              "and re-run.")
        return 0
    print("✓ Monarch session saved to:", result["session_path"])
    if result.get("method") == "token":
        print("  Authenticated via existing session token (Option C).")
        print("  When the token expires, copy a fresh one and re-run.")
    if result["persisted_credentials"]:
        print("✓ Credentials persisted (Option B) for zero-touch reauth.")
        print("  These can be revoked by deleting data/credentials.json and")
        print("  rotating your Monarch password / MFA secret.")
    else:
        print("  Password was NOT stored. Re-run with a fresh --mfa-code on expiry.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
