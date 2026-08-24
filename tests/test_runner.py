from __future__ import annotations

import asyncio
import json
import plistlib
from pathlib import Path

import pytest

from claude_tap.cli import _export_prompt_from_trace
from claude_tap.clients import CODEX_APP_CLIENT, DSH, GEMINI_CLI, OPENCLAW, OPENCODE, QODER
from claude_tap.runner import _resolve_client_command, run_client


def _write_app_bundle(executable: Path, bundle_id: str) -> None:
    executable.parent.mkdir(parents=True)
    executable.write_text("")
    (executable.parent.parent / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_id}))


def test_codexapp_resolves_current_chatgpt_bundle_before_legacy_codex(monkeypatch, tmp_path):
    chatgpt = tmp_path / "ChatGPT.app" / "Contents" / "MacOS" / "ChatGPT"
    codex = tmp_path / "Codex.app" / "Contents" / "MacOS" / "Codex"
    _write_app_bundle(chatgpt, "com.openai.codex")
    _write_app_bundle(codex, "com.openai.codex")
    monkeypatch.setattr(
        "claude_tap.runner._CODEX_APP_EXECUTABLE_CANDIDATES",
        (chatgpt, codex),
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    assert _resolve_client_command(CODEX_APP_CLIENT) == str(chatgpt)


def test_codexapp_ignores_non_codex_chatgpt_bundle(monkeypatch, tmp_path):
    chatgpt = tmp_path / "ChatGPT.app" / "Contents" / "MacOS" / "ChatGPT"
    codex = tmp_path / "Codex.app" / "Contents" / "MacOS" / "Codex"
    _write_app_bundle(chatgpt, "com.openai.chat")
    _write_app_bundle(codex, "com.openai.codex")
    monkeypatch.setattr(
        "claude_tap.runner._CODEX_APP_EXECUTABLE_CANDIDATES",
        (chatgpt, codex),
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    assert _resolve_client_command(CODEX_APP_CLIENT) == str(codex)


@pytest.mark.asyncio
async def test_reverse_mode_child_env_does_not_inherit_outer_proxy(monkeypatch):
    captured: dict = {}

    class FakeProc:
        returncode = None
        pid = 12345

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = 143

        def kill(self) -> None:
            self.returncode = 137

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return FakeProc()

    monkeypatch.setattr("claude_tap.runner.shutil.which", lambda _cmd: "/usr/bin/gemini")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setenv("HTTP_PROXY", "http://outer-proxy.example:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://outer-proxy.example:8080")
    monkeypatch.setenv("ALL_PROXY", "http://outer-proxy.example:8080")

    rc = await run_client(
        client=GEMINI_CLI,
        proxy_port=1234,
        proxy_host="127.0.0.1",
        forward_args=["-p", "hello"],
        proxy_mode="reverse",
        yolo=False,
    )

    assert rc == 0
    env = captured["env"]
    assert env["GOOGLE_GEMINI_BASE_URL"] == "http://127.0.0.1:1234"
    assert env["NO_PROXY"] == "127.0.0.1,localhost"
    assert env["no_proxy"] == "127.0.0.1,localhost"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert key not in env


@pytest.mark.asyncio
async def test_yolo_args_can_be_inserted_after_client_subcommand(monkeypatch):
    captured: dict = {}

    class FakeProc:
        returncode = None
        pid = 12345

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = 143

        def kill(self) -> None:
            self.returncode = 137

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr("claude_tap.runner.shutil.which", lambda _cmd: "/usr/bin/opencode")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    rc = await run_client(
        client=OPENCODE,
        proxy_port=1234,
        proxy_host="127.0.0.1",
        forward_args=["run", "-m", "openai/gpt-4o-mini", "hello"],
        proxy_mode="forward",
        yolo=True,
    )

    assert rc == 0
    assert captured["cmd"][:3] == ("opencode", "run", "--dangerously-skip-permissions")


@pytest.mark.asyncio
async def test_forward_mode_enables_node_environment_proxy(monkeypatch):
    captured: dict = {}

    class FakeProc:
        returncode = None
        pid = 12345

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = 143

        def kill(self) -> None:
            self.returncode = 137

    async def fake_create_subprocess_exec(*_cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return FakeProc()

    monkeypatch.setattr("claude_tap.runner.shutil.which", lambda _cmd: "/usr/bin/dsh")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    rc = await run_client(
        client=DSH,
        proxy_port=1234,
        proxy_host="127.0.0.1",
        forward_args=["--profile", "headless", "hi"],
        proxy_mode="forward",
        yolo=False,
    )

    assert rc == 0
    env = captured["env"]
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:1234"
    assert env["NODE_USE_ENV_PROXY"] == "1"


@pytest.mark.asyncio
async def test_qoder_forward_mode_bypasses_only_static_download_hosts(monkeypatch):
    captured: dict = {}

    class FakeProc:
        returncode = None
        pid = 12345

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = 143

        def kill(self) -> None:
            self.returncode = 137

    async def fake_create_subprocess_exec(*_cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return FakeProc()

    monkeypatch.setattr("claude_tap.runner.shutil.which", lambda _cmd: "/usr/bin/qodercli")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    capture_path = Path("/tmp/qoder-prompt.json")

    rc = await run_client(
        client=QODER,
        proxy_port=1234,
        proxy_host="127.0.0.1",
        forward_args=["--print", "hi"],
        proxy_mode="forward",
        yolo=False,
        capture_prompt_path=capture_path,
    )

    assert rc == 0
    no_proxy = captured["env"]["NO_PROXY"].split(",")
    assert "qoder-ide.oss-accelerate.aliyuncs.com" in no_proxy
    assert "api1.qoder.sh" not in no_proxy
    assert captured["env"]["no_proxy"] == captured["env"]["NO_PROXY"]
    assert "qoder_capture_hook.cjs" in captured["env"]["NODE_OPTIONS"]
    assert captured["env"]["CLAUDE_TAP_QODER_PROMPT_PATH"] == str(capture_path)


@pytest.mark.asyncio
async def test_codexapp_prompts_and_quits_existing_app_before_launch(monkeypatch):
    captured: dict = {}
    quit_called = False

    class FakeProc:
        returncode = None
        pid = 12345

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = 143

        def kill(self) -> None:
            self.returncode = 137

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    def fake_quit_codex_app() -> bool:
        nonlocal quit_called
        quit_called = True
        return True

    monkeypatch.setattr("claude_tap.runner._resolve_client_command", lambda _client: CODEX_APP_CLIENT.cmd)
    monkeypatch.setattr("claude_tap.runner._find_processes_by_command", lambda _cmd: [24680])
    monkeypatch.setattr("claude_tap.runner._quit_codex_app", fake_quit_codex_app)
    monkeypatch.setattr("claude_tap.runner._wait_for_processes_to_exit", lambda _cmd: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    monkeypatch.setattr("sys.stdin", type("Stdin", (), {"isatty": staticmethod(lambda: True)})())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    rc = await run_client(
        client=CODEX_APP_CLIENT,
        proxy_port=1234,
        proxy_host="127.0.0.1",
        forward_args=[],
        proxy_mode="forward",
        yolo=False,
    )

    assert rc == 0
    assert quit_called is True
    assert captured["cmd"] == (CODEX_APP_CLIENT.cmd,)


@pytest.mark.asyncio
async def test_codexapp_declining_existing_app_quit_aborts_launch(monkeypatch):
    launched = False

    async def fake_create_subprocess_exec(*_cmd, **_kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("Codex App should not launch when the existing instance is left running")

    monkeypatch.setattr("claude_tap.runner._resolve_client_command", lambda _client: CODEX_APP_CLIENT.cmd)
    monkeypatch.setattr("claude_tap.runner._find_processes_by_command", lambda _cmd: [24680])
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    monkeypatch.setattr("sys.stdin", type("Stdin", (), {"isatty": staticmethod(lambda: True)})())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    rc = await run_client(
        client=CODEX_APP_CLIENT,
        proxy_port=1234,
        proxy_host="127.0.0.1",
        forward_args=[],
        proxy_mode="forward",
        yolo=False,
    )

    assert rc == 1
    assert launched is False


def test_export_prompt_from_trace_creates_parent_directory(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-21T10:00:00+00:00",
                "request_id": "req_1",
                "turn": 1,
                "request": {
                    "method": "POST",
                    "path": "/v1/messages",
                    "headers": {},
                    "body": {
                        "model": "claude-test",
                        "system": "system text",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                },
                "response": {"status": 502, "headers": {}, "body": {"error": "upstream failed"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "nested" / "prompt.md"

    rc = _export_prompt_from_trace(trace, str(out))

    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "# Prompt Snapshot" not in text
    assert "# System Prompt" in text
    assert "system text" in text


@pytest.mark.asyncio
async def test_openclaw_temp_config_is_removed_after_run(tmp_path, monkeypatch):
    captured: dict = {}
    config = tmp_path / "openclaw.json"
    config.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": {"primary": "local/test"}}},
                "models": {
                    "providers": {"local": {"baseUrl": "https://relay.example.com/v1", "api": "openai-responses"}}
                },
            }
        ),
        encoding="utf-8",
    )

    class FakeProc:
        returncode = None
        pid = 12345

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = 143

        def kill(self) -> None:
            self.returncode = 137

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return FakeProc()

    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config))
    monkeypatch.setattr("claude_tap.runner.shutil.which", lambda _cmd: "/usr/bin/openclaw")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    rc = await run_client(
        client=OPENCLAW,
        proxy_port=1234,
        proxy_host="127.0.0.1",
        forward_args=["agent", "--local", "--message", "hi"],
        proxy_mode="reverse",
        yolo=False,
    )

    assert rc == 0
    temp_config = Path(captured["env"]["OPENCLAW_CONFIG_PATH"])
    assert temp_config != config
    assert not temp_config.exists()
