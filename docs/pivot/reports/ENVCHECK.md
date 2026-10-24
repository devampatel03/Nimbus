# ENVCHECK (P0.6) — run by orchestrator, 2026-10-24

Command: `python scripts/envcheck.py` (real output, not estimated)

| Item | Result | Effect |
|------|--------|--------|
| git repo / origin | ok | — |
| gh auth | ok (account `devampatel03`) | PR → CI → merge flow available (H11 partially satisfied; Actions-enabled not verified) |
| uv 0.8.17, Python 3.14.3 | ok | — |
| Docker CLI 27.0.3 | ok, **daemon not running** (found by `docker info`) | integration tests needing Postgres/Redis/Qdrant cannot run until Docker Desktop is started; P1.2+ needs it |
| Working tree | 25 uncommitted paths | harness-state files left untouched; see HUMAN_TODO H12 |
| TELEGRAM_BOT_TOKEN, _STAGING, ALLOWED_USER_ID | missing | P2.1 live, P6.5 (H2) — fakes until provided |
| LLM_API_KEY / BASE_URL / MODEL | missing | P5.4 bake-off (H3) — FakeLLM until provided |
| GOOGLE_CLIENT_ID/SECRET | missing | P3.4–P3.8 live (H4) — recorded fixtures |
| NOTION_TOKEN | missing | P3.7 live (H5) |
| HEALTHCHECK_PING_URL | missing | P6.4 (H8) |

Nothing blocks W0–W2 build work. Hardware/VM items (H6, H7) are outside envcheck's scope.
