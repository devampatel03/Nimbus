# Egress control runbook (P4.5, R13, ADR-0004)

Status: written on a dev box with no Oracle VM (H6). Nothing in the "VM" sections
below has been run. Items marked UNRUN must be executed and their output saved
as evidence before the P6 staging gate.

## Layers (outermost last to fail)

| # | Layer | Where | What it enforces | Strength |
|---|-------|-------|------------------|----------|
| 1 | `src/net` allowlist | app | Service -> fixed host, https:443 only, path/method/header deny rules, same-host redirects, no env proxy | The real control. Hostname-aware. Bypassed by any code that opens its own socket. |
| 2 | Post-DNS address check | app (`src/net/addrcheck.py`) | Resolved address must be globally routable; loopback, RFC1918, CGNAT, link-local, `169.254.169.254`, multicast, v4-mapped v6 are refused | Closes "allowed name resolves to internal IP". Check-then-connect: a resolver that answers differently the second time is not closed here (layer 3 is the backstop). |
| 3 | Host nftables | VM (`deploy/nftables/nimbus-egress.nft`) | From the app bridge only: DNS to the configured resolvers, TCP 443 (no NTP: the container uses the host clock, chrony on the host), nothing to private/metadata ranges, no IPv6, no container-to-host | Coarse. IP level only. |
| 4 | Compose networks | `docker-compose.yml` | Postgres is on `db` (`internal: true`: no route out, no published port). Only the app joins `egress`. | Strong for Postgres. |
| 5 | OCI security list / NSG | cloud console (H6) | Inbound SSH only. Egress rules optional. | Out of repo; document what was set. |

## The honest limit

nftables cannot filter by hostname. "TCP 443 to the public internet" includes
every site on every CDN. So layer 3 does NOT stop a compromised or injected app
from POSTing data to an attacker's HTTPS server if that code opens its own
socket. What stops that is: only `src/net` imports a transport (INV-1, tested
by import scan), `src/net` pins hosts, and no tool takes a URL (tested:
`tests/unit/tools/test_r13_no_generic_fetch_tool.py`). A fully compromised
Python process can still exfiltrate over 443. Residual risk accepted; do not
claim otherwise. DNS-to-allowed-resolvers can also carry data (DNS tunnelling);
not mitigated beyond pinning the resolvers.

Options that would tighten this, none adopted (would need an ADR 0025): an
egress proxy container doing SNI/hostname allowlisting (the app then has no
direct route at all), or a DNS-based allowlist feeding an nft set.

## Compose usage

- Dev (`docker compose up -d`): loads `docker-compose.override.yml`, which
  publishes Postgres on `127.0.0.1:5432` only. Starts postgres only.
- VM: `docker compose -f docker-compose.yml up -d` (no override, no published
  port). The `app` service is a stub behind profile `app`; P6.1 owns the
  Dockerfile and final definition. The app env file lives at `/etc/nimbus/app.env`,
  mode 0600, outside the repo.
- Because `db` is `internal`, containers on it cannot reach the internet. The
  app reaches Postgres over `db` and the internet over `egress`.
- Note: an app running on the host (not in a container) cannot reach an
  unpublished Postgres. That is deliberate on the VM.

## Hosts the app talks to (for the allowlist reasoning)

`api.telegram.org`, the configured LLM host (`NET_LLM_BASE_URL`), `oauth2.googleapis.com`,
`www.googleapis.com`, `gmail.googleapis.com`, `api.notion.com`, optional healthcheck and
backup hosts. Plus DNS (53) to the pinned resolvers. No NTP from the container: time comes from the host's chrony.

## Install on the VM (UNRUN)

```sh
# 1. syntax check (needs root or CAP_NET_ADMIN)
sudo nft -c -f deploy/nftables/nimbus-egress.nft
# 2. start the stack so the nimbus_egress bridge exists, then load
docker compose -f docker-compose.yml --profile app up -d
sudo nft -f deploy/nftables/nimbus-egress.nft
sudo nft list table inet nimbus_egress
# 3. persist across reboot: include the file from /etc/nftables.conf AFTER docker starts
#    (the bridge must exist when the ruleset loads, or `iifname` simply never matches;
#    use a systemd unit with After=docker.service).
```

Oracle Linux/Ubuntu images may ship iptables rules in `/etc/iptables`; check
`sudo iptables -S` first. Docker uses iptables-nft, so both coexist, but a
REJECT rule in the image's default INPUT chain is a common surprise.

If the OCI VCN resolver (`169.254.169.254`) is used instead of 1.1.1.1/9.9.9.9,
change `RESOLVERS` in the nft file AND `dns:` in compose. The metadata address is
otherwise dropped, by design (the app has no use for instance metadata).

## Verification (UNRUN, run on the VM; save output as evidence)

```sh
APP=$(docker compose -f docker-compose.yml --profile app ps -q app)
# allowed: HTTPS to an allowlisted host (expects an HTTP status, e.g. 302/404)
docker exec $APP python -c "import socket;s=socket.create_connection(('api.telegram.org',443),5);print('443 ok')"
# denied: other TCP port (expects timeout)
docker exec $APP python -c "import socket;socket.create_connection(('1.1.1.1',80),5)"
# denied: metadata and private ranges (expects timeout)
docker exec $APP python -c "import socket;socket.create_connection(('169.254.169.254',80),5)"
docker exec $APP python -c "import socket;socket.create_connection(('10.0.0.1',443),5)"
# denied: container to host sshd (expects timeout/refused)
docker exec $APP python -c "import socket,os;socket.create_connection((os.popen('ip route|awk \"/default/{print \\$3}\"').read().strip(),22),5)"
# postgres has no route out and no published port
docker exec $(docker compose ps -q postgres) sh -c 'timeout 3 bash -c "</dev/tcp/1.1.1.1/443" && echo LEAK || echo blocked'
ss -ltnp | grep 5432 || echo "5432 not listening on host"
# counters / drop log
sudo nft list chain inet nimbus_egress from_app
sudo journalctl -k | grep nimbus-egress-drop | tail
```

Pass criteria: only the first command succeeds; Postgres prints `blocked`; nothing
listens on 5432 on the host.

Tooling note: `bash`/`timeout` exist in the postgres image (Debian base); adjust if
the image changes.

## What was and was not verified (as of this commit)

Verified on the dev box: compose files parse and `docker compose config` resolves
(dev: Postgres published on 127.0.0.1 only; VM form: no ports); `docker compose up
-d` starts postgres healthy; static lint tests of compose and the nft file; post-DNS
address check unit tests (no real DNS); registry test for no generic fetch tool.

NOT verified: the nft ruleset has never been parsed by `nft` (not installed here;
the syntax test skips) nor loaded; interaction with Docker's iptables-nft chains
and chain priority; that the `internal: true` network blocks egress in practice on
the VM; the `app` service (no image exists); the verification commands above;
arm64 behaviour; IPv6 posture on OCI; the OCI security-list side (H6).

## Open items

- Egress proxy with hostname allowlist (the only way to make layer 3 hostname-aware).
- DNS rebinding race (resolve-then-connect) is bounded by layer 3's private-range drop only.
- Alerting on `nimbus-egress-drop` kernel log lines (P4.x/P6.x observability).
