"""Resolve XDG-style paths for CA, config, cached state."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    """Where to store the local CA and other persistent state."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "claude-tap"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "claude-tap"
        return Path.home() / "AppData" / "Roaming" / "claude-tap"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "claude-tap"


def legacy_data_dir() -> Path:
    """Pre-0.2 location of the CA directory."""
    return Path.home() / ".claude-tap"
