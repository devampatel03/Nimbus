# Module settings pattern (P1.5, M10)

Each module owns its configuration in `src/<module>/settings.py`:

- A `pydantic_settings.BaseSettings` subclass with a unique `env_prefix`
  (`LLM_`, `NET_`; new modules pick their own, e.g. `TELEGRAM_`).
- `frozen=True`, `extra="ignore"`, `env_file=".env"`.
- `hide_input_in_errors=True` so a `ValidationError` never echoes a value (INV-7).
- Secrets are `SecretStr`; never log or `repr` their values.
- Constructed where wired (factory / app entrypoint) and passed in; a module
  never reads another module's settings. Cross-module values (the LLM base URL
  is needed by both `llm` and `net`) are read by each from the same env var
  via `AliasChoices`, not by importing the other's settings object.
- Tests build settings with `Settings(_env_file=None, **overrides)`.

`config/settings.py` keeps only what has no module owner yet: Postgres DSN
parts, API auth (`auth_*`, `jwt_*`; goes with `src/api` in P2.1b) and CORS.
Anything else belongs in a module. Env var names go in `.env.example`
(maintained by the orchestrator; it is a shared file).

Logging: call `src.core.logging.install_log_scrubbing(secrets=...)` once at
process start. It scrubs every record process-wide (INV-7).
