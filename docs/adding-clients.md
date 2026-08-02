# Adding a client

The source of truth for supported clients is `claude_tap/clients.py`; protocol
definitions live in `claude_tap/protocols.py`.

## Investigate first

Observe a real run and identify:

- executable and non-interactive invocation;
- API hosts and paths for every authentication mode;
- HTTP, SSE, WebSocket, or proprietary transport;
- base URL or proxy configuration honored by the process;
- request compression and response streaming format;
- bootstrap requests that must succeed before generation begins.

Product documentation is useful, but the actual network behavior is decisive.

## Implement

1. Add a `Client` with its command, supported protocols, mode, configuration
   reader, authentication detection, and launch overrides.
2. Reuse an existing `Protocol` when possible. Otherwise add narrowly scoped
   matching, rewriting, stream reassembly, and usage extraction.
3. Add registry and configuration tests in `tests/test_clients.py`.
4. Add protocol tests in `tests/test_protocols.py`.
5. Add fake-upstream E2E coverage for any new transport or routing behavior.
6. Verify a real trace or prompt export.
7. Update the built-in client table in both README files and `CHANGELOG.md`.

## Verification checklist

- The generation request appears in JSONL, not only model or identity calls.
- The final upstream URL is correct for each authentication and target variant.
- System instructions, messages, tools, tool results, output, and usage render.
- Streaming and non-streaming responses complete cleanly.
- Sensitive headers are redacted.
- User configuration is restored or left untouched after exit.
- Failure messages explain missing executables, credentials, or certificates.

For capture-only integrations, confirm that local bootstrap responses are just
enough to reach prompt generation and cannot be mistaken for upstream output.
