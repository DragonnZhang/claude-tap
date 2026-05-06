"""Version comparison and installer detection."""

from __future__ import annotations

from claude_tap import update


def test_version_tuple_basic():
    assert update.version_tuple("1.2.3") == (1, 2, 3)


def test_version_tuple_strips_local_segment():
    assert update.version_tuple("0.2.0+local") == (0, 2, 0)


def test_version_tuple_handles_prerelease_marker():
    # We accept partial parsing of segments containing trailing letters.
    assert update.version_tuple("1.2rc1") == (1, 2)


def test_is_newer_strict():
    assert update.is_newer("0.3.0", "0.2.99")
    assert not update.is_newer("0.2.0", "0.2.0")
    assert not update.is_newer("0.1.99", "0.2.0")


def test_upgrade_command_uv():
    cmd = update.upgrade_command("uv")
    assert cmd[:3] == ["uv", "tool", "upgrade"]


def test_upgrade_command_pip():
    cmd = update.upgrade_command("pip")
    assert "pip" in cmd
    assert "install" in cmd
    assert "--upgrade" in cmd
