# claude-tap

[![PyPI version](https://img.shields.io/pypi/v/claude-tap.svg)](https://pypi.org/project/claude-tap/)
[![PyPI downloads](https://img.shields.io/pypi/dm/claude-tap.svg)](https://pypi.org/project/claude-tap/)
[![Python version](https://img.shields.io/pypi/pyversions/claude-tap.svg)](https://pypi.org/project/claude-tap/)
[![License](https://img.shields.io/github/license/liaohch3/claude-tap.svg)](https://github.com/liaohch3/claude-tap/blob/main/LICENSE)

Intercept and inspect API traffic from [Claude Code](https://docs.anthropic.com/en/docs/claude-code) or [Codex CLI](https://github.com/openai/codex). See exactly how they construct system prompts, manage history, select tools, and use tokens — in a self-contained HTML trace viewer.

## Install

Requires Python 3.11+ and the client you want to trace (`claude` or `codex`).

```bash
uv tool install claude-tap        # recommended
pip install claude-tap            # or pip
```

## CLI overview

```
claude-tap [global] <command> [opts] [-- args forwarded to the client]

Commands
  run [provider]   Trace a client and launch it (default if omitted)
  proxy            Start the proxy alone (for external clients)
  live             Open the real-time viewer against an existing trace tree
  export FILE      Render a trace JSONL as markdown / json / html
  update           Check for, and optionally install, a new release
  ca {path,...}    Manage the local TLS CA used by forward mode

Global options
  -v, -vv          Increase verbosity (INFO, DEBUG)
  -q, --quiet      Suppress non-error output
  -V, --version    Show version
      --no-color   Disable ANSI colors (also honors NO_COLOR)
      --json       Emit JSON status to stdout where supported
```

A standalone `--` separates `claude-tap`'s own flags from arguments forwarded to the launched client. The first form below means "trace `claude` with the live viewer, and pass `--model …` to `claude`":

```bash
claude-tap -L -- --model claude-opus-4-6
```

If no subcommand is given, `run` is implied — so `claude-tap` and `claude-tap run` are the same.

## Usage

### Claude Code

```bash
claude-tap                                       # trace with defaults
claude-tap -L                                    # + live viewer in browser
claude-tap -- --model claude-opus-4-6            # forward args
claude-tap -- -c                                 # continue last conversation
claude-tap -L -- --dangerously-skip-permissions  # full-power combo
```

### Codex CLI

Codex supports two auth modes with different upstream targets:

| Auth mode | How to authenticate | Upstream target |
|-----------|--------------------|-----------------|
| OAuth (ChatGPT subscription) | `codex login`           | `https://chatgpt.com/backend-api/codex` |
| API key                      | `OPENAI_API_KEY=...`    | `https://api.openai.com` (default)      |

```bash
# OAuth users (must specify target)
claude-tap codex -t https://chatgpt.com/backend-api/codex

# API key users (target works out of the box)
claude-tap codex

# With model and full auto-approval
claude-tap codex -- --model codex-mini-latest --full-auto
```

### Standalone proxy

Start the proxy without launching a client and connect from another terminal:

```bash
claude-tap proxy -p 8080
# In another terminal:
ANTHROPIC_BASE_URL=http://127.0.0.1:8080 claude
```

For Codex:

```bash
claude-tap proxy --provider codex -t https://chatgpt.com/backend-api/codex -p 8080
# Then:
OPENAI_BASE_URL=http://127.0.0.1:8080/v1 codex
```

### Live viewer (history)

```bash
claude-tap live                  # browse historic traces in browser
claude-tap live -p 3000          # fixed port
```

### Export

```bash
claude-tap export trace.jsonl                  # markdown to stdout
claude-tap export trace.jsonl -o out.md        # markdown to file
claude-tap export trace.jsonl --format json
claude-tap export trace.jsonl --format html    # standalone HTML viewer
claude-tap export -                            # read JSONL from stdin
```

### Updates

`run` / `proxy` print a hint when a newer version is on PyPI but never modify the install on their own. Upgrading is opt-in:

```bash
claude-tap update            # check + print
claude-tap update --install  # install via uv or pip (auto-detected)
```

### Local TLS CA (forward mode)

Forward mode terminates TLS so it can read encrypted traffic; that requires a CA the client trusts:

```bash
claude-tap ca path           # print the CA cert path
claude-tap ca install        # show platform-specific trust instructions
claude-tap ca regen          # regenerate the CA
```

The CA lives under `XDG_DATA_HOME/claude-tap` (Linux) or the platform equivalent.

## Architecture

```
                              EventBus
                              /  |  \
                             /   |   \
                  JsonlSink   StatsSink   LiveSink
                                              \
                                               +-- LiveViewerServer (SSE)

  client  --HTTP/WS-->  ReverseProxy  --HTTPS-->  upstream API
                  or
  client  --CONNECT-->  ForwardProxy  --HTTPS-->  upstream API
                       (TLS termination)
```

The proxy never knows about file writers or live viewers — every record is published through an `EventBus` that any number of sinks can subscribe to. Adding a new sink (webhook, Prometheus exporter, …) is one file.

Provider-specific logic (path rewrites, allowed paths, streaming protocol, usage extraction) lives in `claude_tap.providers`. Adding a new provider — e.g. Gemini — is one file: implement a `Provider` and register it.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/
```

The test suite is organised so that:

| File                                | Catches                                                  |
|-------------------------------------|----------------------------------------------------------|
| `test_providers.py`                 | Provider registry, OAuth vs API-key path-strip rules     |
| `test_pipeline.py`                  | Header filtering, redaction, decompression, record shape |
| `test_sse.py`                       | SSE parsing across chunk boundaries; reassembler state   |
| `test_manifest.py`                  | Trace cleanup, legacy `.cloudtap-*` migration            |
| `test_viewer.py`                    | Marker injection, lazy mode threshold, `</script>` escape |
| `test_cli_parsing.py`               | Subcommand dispatch, `--` forwarding, defaults           |
| `test_update.py`                    | Version comparison, installer detection                  |
| `test_logging_setup.py`             | Verbosity → level mapping, no root-logger pollution      |
| `test_export.py`                    | Markdown / JSON / HTML export shape                      |
| `test_e2e_reverse_proxy.py`         | Full proxy flow against a mock upstream (incl. SSE)      |
| `test_e2e_live_viewer.py`           | Live SSE delivery to an HTTP client                      |
| `test_e2e_cli.py`                   | `python -m claude_tap …` end-to-end                      |

Unit tests run in under 2 seconds; the whole suite (including e2e) finishes in well under 5.

## License

MIT
