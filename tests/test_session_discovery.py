"""Tests for multi-path session discovery (_find_session) — the mechanism that
lets the skill share a Monarch session pickle written by an MCP server.

Priority order (see monarch_client._find_session):
  1. the skill's own session pickle
  2. <repo>/.mm/mm_session.pickle   (MCP server run in/beside the repo)
  3. ~/.mm/mm_session.pickle        (MCP server installed elsewhere)
  4. session_path from a config.json walking up from the scripts dir
"""
import pytest

import monarch_client
from monarch_client import _find_session, AuthExpiredError, MonarchClient


def _touch(path, content=b"FAKE_SESSION"):
    path.write_bytes(content)
    return str(path)


def test_finds_skill_session(tmp_path):
    """The skill's own session pickle is used when present (highest priority)."""
    skill = _touch(tmp_path / "session.pickle")
    mcp = _touch(tmp_path / "mcp.pickle")
    home = _touch(tmp_path / "home.pickle")
    found = _find_session(skill, mcp_repo_session=mcp, home_mcp_session=home,
                          config_search_dir=str(tmp_path))
    assert found == skill


def test_finds_mcp_session_fallback(tmp_path):
    """Skill session absent → fall back to the repo-local MCP session."""
    skill = str(tmp_path / "missing.pickle")  # not created
    mcp = _touch(tmp_path / "mcp.pickle")
    home = _touch(tmp_path / "home.pickle")
    found = _find_session(skill, mcp_repo_session=mcp, home_mcp_session=home,
                          config_search_dir=str(tmp_path))
    assert found == mcp


def test_finds_home_mm_fallback(tmp_path):
    """Skill + repo MCP sessions absent → fall back to ~/.mm/mm_session.pickle."""
    skill = str(tmp_path / "missing.pickle")
    mcp = str(tmp_path / "missing_mcp.pickle")
    home = _touch(tmp_path / "home.pickle")
    found = _find_session(skill, mcp_repo_session=mcp, home_mcp_session=home,
                          config_search_dir=str(tmp_path))
    assert found == home


def test_finds_config_json_session(tmp_path):
    """Nothing in the standard spots → honour a config.json session_path."""
    target = _touch(tmp_path / "custom_session.pickle")
    (tmp_path / "config.json").write_text(
        '{"session_path": "%s"}' % target.replace("\\", "\\\\"))
    found = _find_session(
        str(tmp_path / "missing.pickle"),
        mcp_repo_session=str(tmp_path / "no_mcp.pickle"),
        home_mcp_session=str(tmp_path / "no_home.pickle"),
        config_search_dir=str(tmp_path))
    assert found == target


def test_find_session_none_when_absent(tmp_path):
    found = _find_session(
        str(tmp_path / "a.pickle"),
        mcp_repo_session=str(tmp_path / "b.pickle"),
        home_mcp_session=str(tmp_path / "c.pickle"),
        config_search_dir=str(tmp_path))
    assert found is None


def test_no_session_raises(tmp_path, monkeypatch):
    """from_session raises a clear AuthExpiredError when nothing is found."""
    # Deterministic: no session anywhere, regardless of the dev's real ~/.mm.
    monkeypatch.setattr(monarch_client, "_find_session", lambda *a, **k: None)
    with pytest.raises(AuthExpiredError) as exc:
        MonarchClient.from_session(
            session_path=str(tmp_path / "missing.pickle"),
            cache_path=str(tmp_path / "cache.db"))
    assert "setup_auth.py" in str(exc.value)
