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
