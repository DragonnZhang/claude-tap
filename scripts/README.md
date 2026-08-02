# Maintenance scripts

These scripts support optional workflows; normal development only requires the
commands in `docs/development.md`.

- `run_real_e2e.sh`: run Claude Code through the local checkout and validate
  the resulting trace.
- `run_real_e2e_tmux.sh`: exercise the interactive Claude Code UI in tmux.
- `translate_i18n.py`: report or fill missing viewer translations. It requires
  `OPENROUTER_API_KEY` when writing translations.

Run translation discovery without making changes:

```bash
uv run python scripts/translate_i18n.py --dry-run
```
