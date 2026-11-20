# 0012 — Remove the FastAPI/JWT surface entirely; health via heartbeat row and outbound ping

- Status: Accepted. The Brief allowed "minimal localhost health endpoint or REMOVE". This ADR chooses REMOVE. Supersedes MEMORY.md AD-15..18.
- Executed: P2.1b (PR #26, 2026-10-27 UTC). `src/api`, fastapi, uvicorn and pyjwt are removed. Deviations from the Decision below: (1) the heartbeat is one JSON file written atomically by `src/app/heartbeat.py`, not `system_flag.heartbeat_<loop>` rows; `healthcheck` reads the file and does not touch the DB. (2) The `HEALTHCHECK_PING_URL` ping is not built (P6.4, H8).

## Context
`src/api/**` holds a JWT login, REST chat/tasks/memory and a WebSocket. Its only production role is calling `build_graph` (COUPLING §1). Telegram long polling needs no inbound HTTP (R3). A localhost health endpoint would keep `fastapi`, `uvicorn` and a listening socket around just for docker's HEALTHCHECK.

## Decision
- Delete `src/api/**` and `fastapi`, `uvicorn`, `pyjwt`, and the JWT/CORS settings. This happens in P2.1b, after the gateway and the `src/app` entrypoint exist.
- Health works through a heartbeat:
  - Each loop (poller, runner) writes `system_flag.heartbeat_<loop>` every 30 s.
  - `python -m src.app healthcheck` exits non-zero if any heartbeat is older than 90 s or the DB is unreachable. Docker HEALTHCHECK and systemd use it.
  - The runner pings `HEALTHCHECK_PING_URL` every 5 minutes (external uptime, P6.4).
- The future PWA (Brief §6) will add its own HTTP surface behind the `Gateway` protocol when it is built.

## Consequences
- No listening sockets in the app container (R3 is trivially true). Three dependencies and about 470 LOC plus 4 test files are removed.
- Debug memory browsing (`GET /memories`) goes away. A `/export` Telegram command (PRR) and the weekly review replace it.

## Alternatives
- **Keep FastAPI bound to 127.0.0.1 for `/health`.** Rejected: dependency and attack surface for a check that a CLI does equally well.
- **Keep the API for a future PWA.** Rejected: YAGNI. The PWA will need a different auth model (not a shared password) anyway.
