# 0004 — One egress client (`src/net`) with a host allowlist; raw REST, no vendor SDKs; container-level control is coarse

- Status: Accepted, but it **deviates from Brief §2/R13** ("egress allowlist at app + container-network level"). Human review requested (open question Q3).

## Context
R13 requires an outbound allowlist. Vendor SDKs bring their own transports:
- `google-api-python-client` uses `httplib2`. AD-25 noted that this slips past the current INV-1 scan.
- `notion-client` and Telegram libraries manage their own clients.

Docker networks cannot filter by hostname. A real container-level hostname allowlist needs a forward proxy (squid/tinyproxy) inspecting CONNECT/SNI, which adds a second service and its own config to maintain.

## Decision
- `src/net/http.py` is the only importer of `httpx` (and of every other transport, enforced by the INV-1 transport scan). Callers pass a `Service` enum; the client resolves the host from it. Arbitrary URLs are never accepted.
- Allowlist: `api.telegram.org`, the configured LLM host, `oauth2.googleapis.com`, `www.googleapis.com`, `gmail.googleapis.com`, `api.notion.com`, the healthcheck host, and the backup host.
- Telegram (~7 endpoints), Google Calendar/Gmail/Drive and Notion are called as thin hand-written REST clients in `src/integrations/**`. Google token refresh is a plain POST to `oauth2.googleapis.com/token`.
- The `openai` SDK receives an `http_client` built by `net`, so its traffic goes through the same allowlist.
- Network level (P4.5):
  - Postgres sits on an `internal: true` compose network.
  - The app container has outbound tcp/443 plus DNS/NTP only (host nftables).
  - Hostname allowlisting is enforced in the app, not in the network.

## Consequences
- One choke point, unit-testable with `httpx.MockTransport`. Recorded fixtures for Google/Notion are simple.
- More hand-written client code (pagination, retries) than with SDKs. It is bounded, since we use few endpoints.
- Honest limit, which goes in SECURITY.md: compromised native code inside the container could reach any 443 host. The app-level allowlist bounds the *application*, not arbitrary code execution.

## Alternatives
- **Squid sidecar with a domain allowlist.** Deferred as an optional W6 hardening item. It is a second moving part with TLS/SNI subtleties.
- **Vendor SDKs.** Rejected: multiple transports defeat the single choke point and the INV-1/R13 tests.
- **IP-based nftables allowlist.** Rejected: Google and Telegram IP ranges change, and the rules would be brittle.
