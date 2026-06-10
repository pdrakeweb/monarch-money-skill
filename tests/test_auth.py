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
