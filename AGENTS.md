# Contributor guide

Keep this repository easy to verify and maintain.

## Before changing code

1. Check `git status` and preserve unrelated user changes.
2. Read the affected implementation and tests.
3. Use `docs/architecture.md` for system boundaries and
   `docs/adding-clients.md` for client integrations.

## Before committing

Run all required gates:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/ -x --timeout=60
```

Add regression coverage for fixes. For proxy routing changes, use the relevant
fake-upstream E2E test and, when credentials are available, verify one real
request. For viewer interaction changes, run the browser tests when Playwright
is installed and manually inspect a real trace when practical.

## Repository rules

- Code, comments, documentation, and commit messages are English;
  `README_zh.md` is the translation exception.
- Keep commits focused. Do not mix unrelated refactors and behavior changes.
- Do not commit traces, recordings, screenshots, caches, build output, or local
  environments. Attach temporary visual evidence to the pull request.
- Never weaken credential redaction or silently replace a user's configured
  upstream.
- Keep public behavior documented in both README files and `CHANGELOG.md`.
- Push changes on a branch and open a GitHub pull request.

More detail is in `docs/development.md`.
