# Architecture

`claude-tap` launches an AI client behind a local proxy, records its API
traffic, and renders the trace in a self-contained HTML viewer.

## Request flow

```text
client -> reverse or forward proxy -> upstream API
              |
              v
          record builder -> event bus -> JSONL / live viewer / statistics
```

The two proxy modes share the same recording pipeline:

- **Reverse proxy** redirects a client to a local HTTP endpoint. It is the
  simplest option when the client supports a base URL override.
- **Forward proxy** uses `HTTP(S)_PROXY` and a local CA to intercept TLS. It is
  used for desktop applications and clients whose upstream cannot be reliably
  redirected.

## Main modules

| Module | Responsibility |
| --- | --- |
| `cli.py` | Command parsing, target resolution, and lifecycle orchestration |
| `clients.py` | Client launch commands, configuration discovery, and proxy injection |
| `protocols.py` | API path matching, path rewriting, stream decoding, and usage extraction |
| `reverse_proxy.py` | HTTP, SSE, and WebSocket reverse proxy |
| `forward_proxy.py` | CONNECT proxy and TLS interception |
| `pipeline.py` | Transport-independent trace record construction |
| `trace.py` | Event bus and trace sinks |
| `viewer.py` / `viewer.html` | Static trace rendering and browser UI |
| `live_viewer.py` | Live viewer HTTP and SSE server |
| `prompt_snapshot.py` | Normalized prompt export |

## Client and protocol separation

A `Client` describes how to find and launch a product. A `Protocol` describes
the upstream wire format. Keeping these separate lets multiple clients reuse
Anthropic, OpenAI, Gemini, Codex App, or passthrough handling.

When adding a client, prefer an existing protocol. Add protocol-specific logic
only when request matching, stream reassembly, usage extraction, or path
rewriting is genuinely different.

## Trace format

Each JSONL line represents one completed HTTP or WebSocket exchange. It
contains request metadata, a decoded request body when possible, the
reassembled response, raw stream events, timing, and token usage. Sensitive
authorization headers are redacted; request and response bodies are not.

Treat trace files as sensitive because prompts, tool schemas, file contents,
and model output may be present.

## Design constraints

- Preserve a user's configured upstream instead of silently replacing it.
- Keep proxy transports independent from protocol decoding.
- Preserve raw stream events even when normalized rendering is unavailable.
- Keep generated traces and build artifacts out of git.
- Validate routing changes against both fake upstream tests and a real client
  when credentials are available.
