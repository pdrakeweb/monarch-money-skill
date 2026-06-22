"""Unit tests for setup_auth.py and error classification. Written before impl."""
import json
import os
from unittest.mock import MagicMock

import pytest

from monarchmoney import RequireMFAException, LoginFailedException

import setup_auth
from monarch_client import (
    classify_error, AuthExpiredError, NetworkError,
)


def _paths(tmp_path):
    return (
        str(tmp_path / "session.pickle"),
        str(tmp_path / "credentials.json"),
    )


def _fake_mm(session_path):
    """A MagicMock MonarchMoney whose save_session writes a real pickle file."""
    mm = MagicMock()
    mm._headers = {}  # real dict so cookie-header injection is observable

    def _save(path=None):
        with open(path or session_path, "wb") as fh:
            fh.write(b"FAKE_SESSION")
    mm.save_session.side_effect = _save
    return mm


def test_option_a_saves_session_not_password(tmp_path):
    session_path, cred_path = _paths(tmp_path)
    mm = _fake_mm(session_path)
    setup_auth.run_setup(
        email="user@example.com", password="hunter2", mfa_code=None,
        persist=False, session_path=session_path, cred_path=cred_path, mm=mm,
    )
    assert os.path.exists(session_path)
    assert not os.path.exists(cred_path)


def test_option_b_saves_credentials(tmp_path):
    session_path, cred_path = _paths(tmp_path)
    mm = _fake_mm(session_path)
    setup_auth.run_setup(
        email="user@example.com", password="hunter2", mfa_code=None,
        persist=True, session_path=session_path, cred_path=cred_path, mm=mm,
        mfa_secret_key="JBSWY3DPEHPK3PXP",
    )
    assert os.path.exists(cred_path)
    with open(cred_path) as fh:
        creds = json.load(fh)
    assert creds["email"] == "user@example.com"
    assert creds["password"] == "hunter2"
    assert creds["mfa_secret_key"] == "JBSWY3DPEHPK3PXP"


def test_no_password_in_stdout(tmp_path, capsys):
    session_path, cred_path = _paths(tmp_path)
    mm = _fake_mm(session_path)
    setup_auth.run_setup(
        email="user@example.com", password="SuperSecretPw99", mfa_code=None,
        persist=False, session_path=session_path, cred_path=cred_path, mm=mm,
    )
    out = capsys.readouterr().out
    assert "SuperSecretPw99" not in out


def test_mfa_required_calls_multi_factor(tmp_path):
    session_path, cred_path = _paths(tmp_path)
    mm = _fake_mm(session_path)
    mm.login.side_effect = RequireMFAException("mfa required")
    setup_auth.run_setup(
        email="user@example.com", password="hunter2", mfa_code="123456",
        persist=False, session_path=session_path, cred_path=cred_path, mm=mm,
    )
    mm.multi_factor_authenticate.assert_called_once()


def test_error_classification():
    assert isinstance(classify_error(LoginFailedException("bad")), AuthExpiredError)
    assert isinstance(classify_error(ConnectionError("refused")), NetworkError)


def test_option_c_token_saves_session(tmp_path):
    session_path, cred_path = _paths(tmp_path)
    mm = _fake_mm(session_path)
    result = setup_auth.run_setup(
        token="tok_abc123", session_path=session_path, cred_path=cred_path, mm=mm,
    )
    mm.set_token.assert_called_once_with("tok_abc123")
    mm.get_subscription_details.assert_called_once()  # validation ping
    mm.login.assert_not_called()
    assert os.path.exists(session_path)
    assert not os.path.exists(cred_path)
    assert result["method"] == "token"


def test_option_c_token_not_in_stdout(tmp_path, capsys, monkeypatch):
    session_path, cred_path = _paths(tmp_path)
    monkeypatch.setattr(setup_auth, "run_setup", lambda **kw: {
        "session_path": session_path, "persisted_credentials": False,
        "method": "token"})
    code = setup_auth.main(["--token", "tok_supersecret_456"])
    out = capsys.readouterr().out
    assert code == 0
    assert "tok_supersecret_456" not in out


def test_token_with_persist_rejected(tmp_path):
    from monarch_client import MonarchSkillError
    session_path, cred_path = _paths(tmp_path)
    mm = _fake_mm(session_path)
    with pytest.raises(MonarchSkillError):
        setup_auth.run_setup(
            token="tok_abc", persist=True,
            session_path=session_path, cred_path=cred_path, mm=mm,
        )


def test_no_token_and_no_credentials_errors(capsys):
    with pytest.raises(SystemExit):
        setup_auth.main([])


def test_option_d_cookie_native_login_and_pickle(tmp_path):
    session_path, cred_path = _paths(tmp_path)
    mm = _fake_mm(session_path)
    result = setup_auth.run_setup(
        csrf_token="csrf_abc", session_id="sess_xyz",
        session_path=session_path, cred_path=cred_path, mm=mm,
    )
    # Native 1.4 path: login_with_cookies validates, pickle persists.
    mm.login_with_cookies.assert_called_once_with(
        "session_id=sess_xyz; csrftoken=csrf_abc",
        save_session=False, verify=True)
    mm.save_session.assert_called_once_with(session_path)
    mm.login.assert_not_called()
    assert result["method"] == "cookies"
    assert os.path.exists(session_path)
    assert not os.path.exists(cred_path)


def test_option_d_raw_cookie_string(tmp_path):
    session_path, cred_path = _paths(tmp_path)
    mm = _fake_mm(session_path)
    result = setup_auth.run_setup(
        cookie_string="csrftoken=c9 ;  session_id=s9",
        session_path=session_path, cred_path=cred_path, mm=mm,
    )
    mm.login_with_cookies.assert_called_once_with(
        "session_id=s9; csrftoken=c9", save_session=False, verify=True)
    assert result["method"] == "cookies"


def test_parse_cookie_string_legacy_sessionid():
    out = setup_auth.parse_cookie_string("sessionid=old; csrftoken=c")
    assert out == {"session_id": "old", "csrftoken": "c"}


def test_option_d_cookie_string_missing_value_errors(tmp_path):
    from monarch_client import MonarchSkillError
    session_path, cred_path = _paths(tmp_path)
    mm = _fake_mm(session_path)
    with pytest.raises(MonarchSkillError):
        setup_auth.run_setup(
            cookie_string="csrftoken=only_this",
            session_path=session_path, cred_path=cred_path, mm=mm,
        )


def test_option_d_cookie_values_not_in_stdout(tmp_path, capsys, monkeypatch):
    session_path, _ = _paths(tmp_path)
    cookie_path = str(tmp_path / "cookie_session.json")
    monkeypatch.setattr(setup_auth, "run_setup", lambda **kw: {
        "session_path": session_path, "persisted_credentials": False,
        "method": "cookies", "cookie_path": cookie_path})
    code = setup_auth.main(
        ["--csrf-token", "csrf_secret_789", "--session-id", "sess_secret_000"])
    out = capsys.readouterr().out
    assert code == 0
    assert "csrf_secret_789" not in out
    assert "sess_secret_000" not in out


def test_option_d_requires_both_cookies(capsys):
    with pytest.raises(SystemExit):
        setup_auth.main(["--csrf-token", "only_one"])


def test_forbidden_account_create_delete(tmp_path):
    from monarch_client import MonarchClient, MonarchCache, BlockedEndpointError
    inner = MagicMock()
    client = MonarchClient(inner, MonarchCache(":memory:"))
    try:
        with pytest.raises(BlockedEndpointError):
            client.mm.delete_account("acc_001")
        with pytest.raises(BlockedEndpointError):
            client.mm.create_manual_account
        inner.delete_account.assert_not_called()
        inner.create_manual_account.assert_not_called()
    finally:
        client.cache.close()


def test_forbidden_cannot_be_armed():
    from monarch_client import MonarchClient, MonarchCache, BlockedEndpointError
    client = MonarchClient(MagicMock(), MonarchCache(":memory:"))
    try:
        with pytest.raises(BlockedEndpointError):
            with client.mm.allow("delete_account"):
                pass
        with pytest.raises(BlockedEndpointError):
            with client.mm.allow("create_manual_account"):
                pass
    finally:
        client.cache.close()


def test_mutations_require_confirmation():
    from monarch_client import (
        MonarchClient, MonarchCache, ConfirmationRequiredError,
    )
    client = MonarchClient(MagicMock(), MonarchCache(":memory:"))
    try:
        for name in ("delete_transaction", "create_transaction",
                     "update_account", "reset_budget", "set_budget_amount"):
            with pytest.raises(ConfirmationRequiredError):
                getattr(client.mm, name)
    finally:
        client.cache.close()


def test_armed_mutation_passes_through_once():
    from monarch_client import (
        MonarchClient, MonarchCache, ConfirmationRequiredError,
    )
    inner = MagicMock()
    client = MonarchClient(inner, MonarchCache(":memory:"))
    try:
        with client.mm.allow("reset_budget"):
            client.mm.reset_budget("budget_1")
        inner.reset_budget.assert_called_once_with("budget_1")
        # Arming is scoped to the with-block — gone afterwards.
        with pytest.raises(ConfirmationRequiredError):
            client.mm.reset_budget
        # Arming one method does not arm another.
        with client.mm.allow("set_budget_amount"):
            with pytest.raises(ConfirmationRequiredError):
                client.mm.delete_transaction
    finally:
        client.cache.close()


def test_allow_rejects_read_methods():
    from monarch_client import MonarchClient, MonarchCache
    client = MonarchClient(MagicMock(), MonarchCache(":memory:"))
    try:
        with pytest.raises(ValueError):
            client.mm.allow("get_accounts")
    finally:
        client.cache.close()


def test_read_methods_pass_through():
    from monarch_client import MonarchClient, MonarchCache
    inner = MagicMock()
    client = MonarchClient(inner, MonarchCache(":memory:"))
    try:
        client.mm.get_accounts()
        inner.get_accounts.assert_called_once()
        # internal attrs forward too (cookie auth pokes _headers)
        assert client.mm._headers is inner._headers
    finally:
        client.cache.close()


def test_ensure_authenticated_falls_back_to_env_cookies(tmp_path):
    """Zero-touch desktop pattern: a .env in the persistent data dir with
    MONARCH_COOKIES lets every new chat session re-auth without asking."""
    from monarch_client import MonarchClient
    env_path = tmp_path / ".env"
    env_path.write_text('MONARCH_COOKIES="session_id=es1; csrftoken=ec1"\n')
    client = MonarchClient.ensure_authenticated(
        session_path=str(tmp_path / "missing.pickle"),
        cred_path=str(tmp_path / "missing_creds.json"),
        cache_path=str(tmp_path / "cache.db"),
        cookie_path=str(tmp_path / "missing_cookies.json"),
        env_path=str(env_path),
    )
    try:
        assert client.mm._cookies == {"session_id": "es1", "csrftoken": "ec1"}
        assert client.mm._headers["X-Csrftoken"] == "ec1"
    finally:
        client.cache.close()


def test_ensure_authenticated_no_auth_raises(tmp_path, monkeypatch):
    from monarch_client import MonarchClient
    # Isolate the secrets vault fallback too: point it at an empty dir so
    # a real published vault on the dev machine can't satisfy auth here.
    monkeypatch.setenv("CLAUDE_SECRETS_DIR", str(tmp_path / "novault"))
    with pytest.raises(AuthExpiredError):
        MonarchClient.ensure_authenticated(
            session_path=str(tmp_path / "a.pickle"),
            cred_path=str(tmp_path / "b.json"),
            cache_path=str(tmp_path / "c.db"),
            cookie_path=str(tmp_path / "d.json"),
            env_path=str(tmp_path / ".env"),
        )


def test_ensure_authenticated_falls_back_to_legacy_cookie_file(tmp_path):
    from monarch_client import MonarchClient
    cookie_path = str(tmp_path / "cookie_session.json")
    # Legacy pre-1.4 sidecar with the old Django cookie name.
    with open(cookie_path, "w") as fh:
        json.dump({"csrftoken": "c1", "sessionid": "s1"}, fh)
    client = MonarchClient.ensure_authenticated(
        session_path=str(tmp_path / "missing.pickle"),
        cred_path=str(tmp_path / "missing_creds.json"),
        cache_path=str(tmp_path / "cache.db"),
        cookie_path=cookie_path,
    )
    try:
        # Native set_cookies path: cookie dict adopted, CSRF header set.
        assert client.mm._cookies == {"session_id": "s1", "csrftoken": "c1"}
        assert client.mm._headers["X-Csrftoken"] == "c1"
    finally:
        client.cache.close()


# ---- secrets vault fallback (from_vault) --------------------------- #
def _write_vault(tmp_path, monkeypatch, value="session_id=abc; csrftoken=xyz"):
    store = tmp_path / "_secrets"
    store.mkdir()
    (store / "secrets.json").write_text(
        json.dumps({"secrets": {"monarch_cookies": {"value": value}}}),
        encoding="utf-8")
    monkeypatch.setenv("CLAUDE_SECRETS_DIR", str(store))


def test_from_vault_adopts_cookie_secret(tmp_path, monkeypatch):
    from monarch_client import MonarchClient
    _write_vault(tmp_path, monkeypatch)
    client = MonarchClient.from_vault(
        cache_path=str(tmp_path / "c.db"),
        session_path=str(tmp_path / "s.pickle"))
    assert client is not None and client.cache is not None


def test_from_vault_absent_raises(tmp_path, monkeypatch):
    from monarch_client import MonarchClient
    monkeypatch.setenv("CLAUDE_SECRETS_DIR", str(tmp_path / "nope"))
    with pytest.raises(AuthExpiredError):
        MonarchClient.from_vault(
            cache_path=str(tmp_path / "c.db"),
            session_path=str(tmp_path / "s.pickle"))


def test_secret_provider_reads_vault(tmp_path, monkeypatch):
    import secret_provider as sp
    _write_vault(tmp_path, monkeypatch, value="session_id=zzz; csrftoken=qqq")
    assert sp.vault_available() is True
    assert sp.get_secret("monarch_cookies") == "session_id=zzz; csrftoken=qqq"
    assert sp.secret_names() == ["monarch_cookies"]
    assert sp.get_secret("nope") is None
