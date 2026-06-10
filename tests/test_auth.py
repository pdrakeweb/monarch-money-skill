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


def test_option_d_cookie_saves_cookie_session(tmp_path):
    session_path, cred_path = _paths(tmp_path)
    cookie_path = str(tmp_path / "cookie_session.json")
    mm = _fake_mm(session_path)
    result = setup_auth.run_setup(
        csrf_token="csrf_abc", session_id="sess_xyz",
        session_path=session_path, cred_path=cred_path,
        cookie_path=cookie_path, mm=mm,
    )
    # Headers injected for Django session auth.
    assert "csrftoken=csrf_abc" in mm._headers["Cookie"]
    assert "sessionid=sess_xyz" in mm._headers["Cookie"]
    assert mm._headers["X-CSRFToken"] == "csrf_abc"
    assert mm._headers["Origin"] == "https://app.monarchmoney.com"
    mm.get_subscription_details.assert_called_once()  # validation ping
    mm.login.assert_not_called()
    mm.save_session.assert_not_called()  # no bearer token to pickle
    assert result["method"] == "cookies"
    with open(cookie_path) as fh:
        saved = json.load(fh)
    assert saved == {"csrftoken": "csrf_abc", "sessionid": "sess_xyz"}
    assert not os.path.exists(cred_path)


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


def test_blocked_account_create_delete(tmp_path):
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


def test_blocked_other_destructive_endpoints():
    from monarch_client import MonarchClient, MonarchCache, BlockedEndpointError
    client = MonarchClient(MagicMock(), MonarchCache(":memory:"))
    try:
        for name in ("delete_transaction", "create_transaction",
                     "update_account", "reset_budget", "set_budget_amount"):
            with pytest.raises(BlockedEndpointError):
                getattr(client.mm, name)
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


def test_ensure_authenticated_falls_back_to_cookie_session(tmp_path):
    from monarch_client import MonarchClient
    cookie_path = str(tmp_path / "cookie_session.json")
    with open(cookie_path, "w") as fh:
        json.dump({"csrftoken": "c1", "sessionid": "s1"}, fh)
    client = MonarchClient.ensure_authenticated(
        session_path=str(tmp_path / "missing.pickle"),
        cred_path=str(tmp_path / "missing_creds.json"),
        cache_path=str(tmp_path / "cache.db"),
        cookie_path=cookie_path,
    )
    try:
        assert "sessionid=s1" in client.mm._headers["Cookie"]
        assert client.mm._headers["X-CSRFToken"] == "c1"
    finally:
        client.cache.close()
