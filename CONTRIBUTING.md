# Contributing to tai-channel-telegram

`tai-channel-telegram` is a Telegram **channel** plugin for the TAI ecosystem:
`ask_user(..., channel="telegram")` delivers the question to a Telegram chat and
bridges the typed reply back to the interaction's public callback door. The hard
rule (the plugin rule): **it depends on `tai-contract` + `tai-kit` only and never
imports the skeleton.** The skeleton loads it through the manifest's
`channel_modules` field; `tai_channel_telegram.register` registers the
`"telegram"` channel and its inbound route as a side-effect — there is no import
edge to the skeleton in either direction.

## Ground rules

- **No skeleton import — ever.** The package is contract-facing; the ban is
  enforced by ruff (`flake8-tidy-imports`), so a stray import fails lint:
  ```bash
  grep -rn "tai_skeleton" src/   # must be empty
  ```
- **Credentials are operator-bound, never LLM-visible.** The bot token and
  recipient configuration come from the environment, never from a tool
  parameter.
- **Fail closed.** A delivery or inbound event against an unconfigured channel
  raises loudly, naming the missing env var, rather than silently dropping the
  question or the reply.
- **Typed package** (`py.typed`). Pyright runs clean.

## Layout

- `register.py` — registers the `"telegram"` channel and the inbound route as an
  import side-effect.
- `channel.py` — the outbound `Channel` implementation.
- `inbound.py` — the inbound door that bridges the typed reply back to the
  callback.
- `client.py`, `correlation.py`, `settings.py` — the HTTP client, the Redis
  correlation store, and the `CHANNEL_TELEGRAM_` settings.

## Dev

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

For local cross-repo work, `make dev` editable-installs the sibling `tai-*`
checkouts this package builds on into the venv. While `[tool.uv.sources]` pins
those siblings to local paths, `uv sync` already installs them editable and
`make dev` changes nothing; once the lock resolves them from the registry,
`uv sync` / `uv run` installs the published builds instead, so re-run
`make dev` afterward to restore the editable links.

Before any commit, run a secret scan over `src/` and `tests/` (e.g.
`detect-secrets scan`).

## License

By contributing you agree your contributions are licensed under Apache-2.0.
