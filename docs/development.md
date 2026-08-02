# Development

## Setup

```bash
uv sync --extra dev
```

Run the local checkout without installing it globally:

```bash
uv run claude-tap --help
```

## Required checks

Before committing:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/ -x --timeout=60
```

Use focused tests while developing, then run the full suite. Proxy and routing
changes should include the relevant fake-upstream E2E tests. Run a real client
through the proxy when credentials and the client are available.

## Repository layout

```text
claude_tap/   package source and bundled viewer
tests/        unit, browser, and fake-upstream E2E tests
scripts/      optional real-E2E and translation helpers
docs/         stable architecture and maintenance documentation
```

Generated directories such as `.traces/`, `build/`, `dist/`, `*.egg-info`,
cache directories, recordings, and screenshots are intentionally ignored.

## Change guidelines

- Keep code, comments, documentation, and commit messages in English. The
  translated `README_zh.md` is the exception.
- Keep a change focused and include a regression test for bug fixes.
- Do not commit real traces. Add screenshots only when a PR needs visual
  review; attach them to the PR instead of retaining them as repository docs.
- Read the package version from import metadata; do not duplicate it in code.
- Preserve compatibility with Python 3.11 through 3.13.
- For TLS changes, retain SKI and AKI certificate extensions required by newer
  Python/OpenSSL combinations.

## Viewer changes

The viewer is a self-contained HTML application. Keep protocol-specific data
normalization at clear boundaries and preserve rendering of older traces.

For interaction changes, extend `tests/test_viewer.py` or
`tests/test_viewer_scroll_browser.py`. Check keyboard navigation, narrow
viewports, overflow behavior, and both light and dark themes when relevant.

The translation helper reports and fills missing viewer locale keys:

```bash
uv run python scripts/translate_i18n.py --dry-run
```

## Debugging proxy failures

Trace the code path from client configuration to the final upstream URL.
Compare a working client with the failing one and verify:

1. the client inherited the intended proxy or base URL configuration;
2. the actual transport is HTTP, SSE, WebSocket, or CONNECT as expected;
3. the fully constructed upstream URL is correct;
4. compression and content encoding were decoded;
5. the trace contains the generation request, not only bootstrap traffic.

Fake upstreams prove internal behavior but cannot prove that a real product has
not changed its endpoint, authentication flow, or transport.
