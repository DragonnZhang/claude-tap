# claude-tap — agent / contributor guide

This file is the developer-facing knowledge base for `claude-tap`. The
public README is for users; this file is for coding agents and
contributors who need to extend, debug, or consume the proxy.

For workflow / review / commit policy, see [`AGENTS.md`](AGENTS.md).
This document covers **architecture, extension points, and contracts**.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  cli.py            argparse entry; resolves target + mode       │
├─────────────────────────────────────────────────────────────────┤
│  clients.py        N CLI launchers (claude/codex/gemini/...)    │
│  protocols.py      M wire formats (anthropic/openai/gemini/     │
│                    passthrough); orthogonal to clients          │
├─────────────────────────────────────────────────────────────────┤
│  reverse_proxy.py  aiohttp web app; rewrite_upstream_path,      │
│                    streaming SSE relay, WebSocket bridge        │
│  forward_proxy.py  raw asyncio CONNECT + per-host TLS-MITM      │
│  pipeline.py       transport-agnostic record builder            │
├─────────────────────────────────────────────────────────────────┤
│  trace.py          EventBus pub/sub                             │
│    JsonlSink         → writes trace_HHMMSS.jsonl                │
│    StatsSink         → in-memory token + model accounting       │
│    LiveSink          → forwards to LiveViewerServer (SSE)       │
├─────────────────────────────────────────────────────────────────┤
│  viewer.py         renders trace.jsonl → standalone .html       │
│  live_viewer.py    real-time browser UI over SSE                │
│  certs.py          local CA + per-host leaf cert minting        │
└─────────────────────────────────────────────────────────────────┘
```

### Core abstraction: Client × Protocol

Two orthogonal axes, kept separate so adding either side is a single
file change:

* **Protocol** (`protocols.py`) — owns one upstream API's wire format.
  Fields: `name`, `default_target`, `allowed_paths`, `is_streaming`,
  `rewrite_upstream_path`, `make_reassembler`, `extract_usage`.
  Concrete protocols: `ANTHROPIC`, `OPENAI`, `GEMINI`, `PASSTHROUGH`.
* **Client** (`clients.py`) — owns a CLI binary's launch metadata.
  Fields: `name`, `cmd`, `label`, `install_url`, `protocols` (1+),
  `env_overrides(proxy_url)`, `cli_args_overrides(proxy_url, env)`,
  `read_configured_upstream(env)`, `env_redirect_reliable: bool`,
  `pre_launch_env_purge`, `detect_auth()`.

A client may declare multiple protocols. `opencode` carries
`(ANTHROPIC, OPENAI, GEMINI, PASSTHROUGH)` so requests are routed by
path to the matching protocol's reassembler.

### Two transports, one pipeline

`pipeline.py` is transport-agnostic; both proxies feed it the same
record builder (`build_http_record` / `build_ws_record`):

* **Reverse proxy** (`reverse_proxy.py`) — aiohttp web app on
  `127.0.0.1:<port>`. Set as the child's `*_BASE_URL` env or via
  CLI arg. Forwards each request to a fixed `ctx.target`.
* **Forward proxy** (`forward_proxy.py`) — raw `asyncio.start_server`
  handling `CONNECT host:443`. Uses a temporary loopback TLS server
  to terminate TLS without `loop.start_tls` (which is unreliable on
  some Python builds). The CONNECT host is the real upstream — no
  static `--target` needed.

### EventBus pub/sub

`trace.py` defines `EventBus` and the `Sink` Protocol. The proxies
only call `bus.publish(record)`; sinks subscribe independently.
This decouples the proxy from delivery — adding a webhook or
Prometheus exporter is a new `Sink` class, not a proxy change.

---

## Trace record schema

Every captured request becomes one JSON line in the `.jsonl` output:

```json
{
  "timestamp": "2026-05-06T12:01:37.410+00:00",
  "request_id": "req_c3b5776232ce",
  "turn": 1,
  "duration_ms": 1088,
  "request": {
    "method": "POST",
    "path": "/v1/messages?beta=true",
    "headers": { "Authorization": "Bearer sk-an...", "...": "..." },
    "body": { "model": "claude-haiku-4-5", "messages": [...] }
  },
  "response": {
    "status": 200,
    "headers": { "content-type": "text/event-stream", "...": "..." },
    "body": {
      "id": "msg_xxx",
      "content": [{"type": "text", "text": "hello"}],
      "stop_reason": "end_turn",
      "usage": { "input_tokens": 352, "output_tokens": 15 }
    },
    "sse_events": [
      {"event": "message_start", "data": {...}},
      {"event": "content_block_delta", "data": {...}}
    ]
  },
  "upstream_base_url": "https://api.anthropic.com"
}
```

Notes for consumers:

* `body` (response) is the **reassembled snapshot** — equivalent to a
  non-streaming reply. The streaming chunks are in `sse_events` (or
  `ws_events` for WebSocket transport).
* Sensitive headers (`Authorization`, `x-api-key`) are redacted to
  the first 12 characters at write time.
* `upstream_base_url` is the actual host hit (CONNECT host in forward
  mode, `--target` in reverse mode), not whatever was in the URL.
* Passthrough protocols (Cursor / Qoder / Devin) leave `body: null`
  because we don't reassemble their proprietary format; the raw
  events are still in `sse_events`.

---

## Target & mode resolution

`cli.py: resolve_target_and_mode` is a pure function. Priority:

```
target = --target
       ?? client.read_configured_upstream(env)   ← reads user's actual base_url
       ?? auth.suggested_target                   ← derived from login state
       ?? protocol.default_target
```

```
mode = --mode
     ?? "reverse" if client.env_redirect_reliable
     ?? "forward"  (multi-backend clients whose config-file baseURL
                    overrides env)
```

`env_redirect_reliable=False` is set on `OPENCODE`, `PI`, `KIMI`,
`IFLOW`, `HERMES` — for these, env-based redirect silently fails when
the user has a config-file baseURL set, so the resolver always picks
forward mode.

---

## Configuration sources (where each CLI's base_url lives)

Verified against each CLI's actual source code. Used by
`Client.read_configured_upstream` to make `claude-tap` transparent.

| CLI       | Source for `base_url` (priority order)                            |
|-----------|-------------------------------------------------------------------|
| claude    | `ANTHROPIC_BASE_URL` env                                          |
| codex     | `~/.codex/config.toml` → `model_provider` → `model_providers.<X>.base_url` |
| gemini    | `GOOGLE_GEMINI_BASE_URL` / `GOOGLE_VERTEX_BASE_URL` / `CODE_ASSIST_ENDPOINT` env |
| opencode  | `~/.config/opencode/opencode.json` → `provider.<active>.options.baseURL` (active = first part of `model: <provider>/<id>`) |
| pi        | `~/.pi/agent/models.json` → `providers.<defaultProvider>.baseUrl` |
| kimi      | `~/.kimi/config.toml` → `default_model` → `models.<m>.provider` → `providers.<p>.base_url` |
| iflow     | `~/.iflow/settings.json` → `baseUrl` (settings wins over env)     |
| hermes    | `~/.hermes/config.yaml` → `model.base_url` (with per-provider env override for OpenRouter) |
| cursor    | `CURSOR_API_BASE_URL` env                                         |
| qoder     | `QODER_CENTER_DOMAIN` env                                         |
| devin     | (not user-customizable; rustls binary)                            |

---

## Codex special-case

Codex (Rust binary) **does not honor** `OPENAI_BASE_URL` env. Built-in
provider IDs (`openai`, `azure`, `oss`) **cannot be overridden** via
`[model_providers.openai]` (codex rejects same-named blocks as
reserved). `_codex_cli_args` picks one of three redirect mechanisms:

1. **User has a custom `model_provider`** with their own block —
   override only `base_url` via `-c model_providers.<active>.base_url=…`
2. **Built-in default** (no `model_provider` set, or set to `openai`) —
   use top-level `-c openai_base_url="<proxy>/v1"`. Works for both
   API-key and ChatGPT-OAuth flows.

`chatgpt_base_url` exists too but only routes ChatGPT web endpoints
(plugins / connectors), not the LLM API — don't use it for capture.

---

## Extending

### Add a new CLI

Edit `claude_tap/clients.py`:

```python
def _mycli_env(proxy_url: str) -> dict[str, str]:
    return {"MYCLI_BASE_URL": proxy_url}

def _mycli_configured(env: Mapping[str, str]) -> str | None:
    return _strip_url(env.get("MYCLI_BASE_URL"))

def _mycli_auth() -> AuthInfo:
    if os.environ.get("MYCLI_API_KEY"):
        return AuthInfo(
            logged_in=True, mode="apikey",
            detail="MYCLI_API_KEY env var",
            suggested_target="https://api.mycli.example.com",
        )
    return AuthInfo(logged_in=False, mode="unknown",
                    detail="not logged in (export MYCLI_API_KEY)")

MYCLI = Client(
    name="mycli",
    cmd="mycli",
    label="My CLI",
    install_url="https://github.com/me/mycli",
    protocols=(OPENAI,),
    env_overrides=_mycli_env,
    read_configured_upstream=_mycli_configured,
    detect_auth=_mycli_auth,
)
```

Add `MYCLI` to the `_REGISTRY` tuple at the bottom. Add a unit test
in `tests/test_clients.py`. Done — the CLI is now usable as
`claude-tap mycli`.

If the CLI is multi-backend (config-file baseURL wins over env), set
`env_redirect_reliable=False` and let forward mode handle redirect.

If env vars don't redirect at all (codex case), implement
`cli_args_overrides(proxy_url, env)` to inject the right CLI flags.

### Add a new sink

Implement the `Sink` protocol from `trace.py`:

```python
class WebhookSink:
    def __init__(self, url: str) -> None:
        self.url = url
        self._session = aiohttp.ClientSession()

    async def handle(self, record: dict) -> None:
        await self._session.post(self.url, json=record)

    async def close(self) -> None:
        await self._session.close()
```

Wire it up in `cli.py` next to `JsonlSink` / `StatsSink`:

```python
bus.subscribe(WebhookSink(args.webhook_url))
```

### Add a new protocol

Add a reassembler in `sse.py` if the upstream's SSE shape isn't
already covered. Then in `protocols.py`:

```python
MYAPI = Protocol(
    name="myapi",
    default_target="https://api.myapi.example.com",
    allowed_paths=("/v1/chat",),
    make_reassembler=MyAPIReassembler,
    extract_usage=lambda body: Usage(...),
)
```

Add to `_REGISTRY` and reference from any client that speaks it.

---

## Development

```bash
uv sync --extra dev
uv run ruff check claude_tap tests
uv run ruff format --check claude_tap tests
uv run pytest tests/
```

Pre-commit hook (recommended):

```bash
git config core.hooksPath .githooks
```

### Test layout

| File                            | Catches                                                      |
|---------------------------------|--------------------------------------------------------------|
| `test_clients.py`               | Client registry, env_overrides, configured-upstream readers  |
| `test_protocols.py`             | Protocol matching, path rewrites, usage extraction           |
| `test_pipeline.py`              | Header filtering, redaction, decompression, record shape     |
| `test_sse.py`                   | SSE parsing across chunk boundaries; reassembler state       |
| `test_cli_parsing.py`           | Subcommand dispatch, `--` forwarding, target/mode resolution |
| `test_manifest.py`              | Trace cleanup, legacy `.cloudtap-*` migration                |
| `test_viewer.py`                | Marker injection, lazy mode threshold, `</script>` escape    |
| `test_export.py`                | Markdown / JSON / HTML export shape                          |
| `test_logging_setup.py`         | Verbosity → level mapping, no root-logger pollution          |
| `test_update.py`                | Version comparison, installer detection                      |
| `test_e2e_reverse_proxy.py`     | End-to-end reverse-mode proxy against a mock upstream        |
| `test_e2e_forward_proxy.py`     | End-to-end forward-mode TLS-MITM with CA + leaf certs        |
| `test_e2e_live_viewer.py`       | Live SSE delivery to an HTTP client                          |
| `test_e2e_cli.py`               | `python -m claude_tap …` end-to-end                          |

Unit tests run in well under 2 seconds; the full suite (with e2e) in
under 5.

---

## Security notes

* `claude-tap` sees **everything** the child CLI sends, including OAuth
  tokens and API keys. Sensitive headers are redacted to the first 12
  characters at write time, but the request body and trace output are
  never sanitized — don't share traces from production sessions
  without scrubbing.
* The local CA's private key (`ca-key.pem`, mode 0600) signs leaf
  certs the child trusts. Anyone who reads it can MITM the child
  process. Don't check it into git, don't share it.
* Forward mode requires the child to trust our CA. If a CLI ever
  validates an unrelated trust path (cert pinning, OS-only trust),
  the proxied connection will be refused — by design.

---

## Repo policy

For commit / PR / review rules, see [`AGENTS.md`](AGENTS.md).
TL;DR: every commit must pass `ruff check`, `ruff format --check`, and
`pytest tests/ -x --timeout=60`. One concern per commit. English in
code/comments/docs. Push branches and open PRs via `gh pr create`.
