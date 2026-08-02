# claude-tap maintainer notes

`claude-tap` launches supported AI clients through a local reverse or forward
proxy, captures HTTP/SSE/WebSocket traffic as JSONL, and renders a standalone
HTML viewer.

Start with:

- [`AGENTS.md`](AGENTS.md) for contribution rules and required checks;
- [`docs/architecture.md`](docs/architecture.md) for module boundaries and data
  flow;
- [`docs/development.md`](docs/development.md) for setup, testing, viewer work,
  and debugging;
- [`docs/adding-clients.md`](docs/adding-clients.md) when integrating a client.

The implementation is the source of truth:

- `claude_tap/clients.py` defines launch and configuration behavior;
- `claude_tap/protocols.py` defines API matching and stream handling;
- `claude_tap/pipeline.py` builds transport-independent trace records;
- `claude_tap/viewer.html` contains the self-contained viewer;
- `tests/` documents supported behavior through executable examples.

Do not duplicate client matrices, trace schemas, or implementation details in
this file. Update the focused documentation only when a stable contract changes.
