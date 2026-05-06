"""PyPI update check and (opt-in) upgrade.

The ``run`` and ``proxy`` subcommands only *check* and print a hint. Actually
installing the new version is restricted to the ``update`` subcommand so
that a debugging tool never silently rewrites itself behind the user's back.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from typing import Iterable

from claude_tap._version import __version__

PYPI_URL = "https://pypi.org/pypi/claude-tap/json"


def version_tuple(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in v.strip().split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            parts.append(int(digits))
    return tuple(parts)


def is_newer(remote: str, local: str = __version__) -> bool:
    return version_tuple(remote) > version_tuple(local)


async def latest_version(timeout: float = 3.0) -> str | None:
    url = os.environ.get("CLAUDE_TAP_PYPI_URL", PYPI_URL)

    def _fetch() -> str | None:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                ver = (data.get("info") or {}).get("version")
                return ver if isinstance(ver, str) else None
        except Exception:
            return None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch)


def detect_installer() -> str:
    exe = sys.executable or ""
    if "uv" in exe.lower() or shutil.which("uv"):
        return "uv"
    return "pip"


def upgrade_command(installer: str) -> list[str]:
    if installer == "uv":
        return ["uv", "tool", "upgrade", "claude-tap"]
    return [sys.executable, "-m", "pip", "install", "--upgrade", "claude-tap"]


def run_upgrade(installer: str | None = None, *, capture: bool = False) -> int:
    cmd = upgrade_command(installer or detect_installer())
    sys.stdout.write(f"[claude-tap] running: {' '.join(cmd)}\n")
    sys.stdout.flush()
    if capture:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        sys.stdout.write(proc.stdout)
        return proc.returncode
    return subprocess.call(cmd)


def hint(latest: str, *, channel: str = "stderr") -> Iterable[str]:
    out = (
        f"[claude-tap] update available: {__version__} -> {latest}",
        f"[claude-tap] run `claude-tap update` to install ({detect_installer()})",
    )
    stream = sys.stderr if channel == "stderr" else sys.stdout
    for line in out:
        stream.write(line + "\n")
    stream.flush()
    return out
