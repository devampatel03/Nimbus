# Deploy, cutover and rollback (P6.1 / P6.2)

Status: UNRUN. Written without a VM (H6). Every command below is unverified on
a real host; run it on STAGING first and save the output as evidence. Never
deploy from a task branch; deploy a tagged commit of `pivot`/`main`.

## What is in the box

| Piece | File |
|---|---|
| App image (non-root, read-only FS, arm64-capable, no secrets/weights) | `Dockerfile`, `.dockerignore` |
| Compose: `postgres`, `app` (profile `app`), `migrate` (profile `ops`) | `docker-compose.yml` |
| VM bootstrap (idempotent) | `deploy/bootstrap-vm.sh` |
| Boot unit (compose up + egress ruleset) | `deploy/nimbus.service` |
| Egress backstop | `deploy/nftables/nimbus-egress.nft`, `docs/runbooks/egress.md` |
| DB roles | ADR-0026, `src/db/roles.py`, `infra/docker/postgres/20-app-role.sh` |

CI builds the image for `linux/arm64` under QEMU (build only, no push) and
shellchecks the scripts. Local equivalent:
`docker buildx build --platform linux/arm64 -t nimbus-app:local .`

Local embedder: NOT in the image (torch is multi-GB, weights download at run
time). Default is `APP_EMBEDDER=api`. To opt in, build with
`--build-arg UV_EXTRAS="--extra local-embed"`, mount a persistent `HF_HOME`
volume and raise `mem_limit`; the 2g app limit is sized for the API embedder.

## Two env files (never in the repo, root:root 0600)

`/etc/nimbus/migrate.env` (owner; used by compose interpolation and `migrate`):
`POSTGRES_HOST=postgres`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`,
`POSTGRES_APP_USER=nimbus_app`, `POSTGRES_APP_PASSWORD`.

`/etc/nimbus/app.env` (runtime): everything in `docs/pivot/ENV_CHECKLIST.md`
(Telegram, LLM, `NIMBUS_DATA_KEY`, Google/Notion if used) plus
`POSTGRES_HOST=postgres`, `POSTGRES_DB`, `POSTGRES_APP_USER`,
`POSTGRES_APP_PASSWORD`. It must NOT contain `POSTGRES_USER`/`POSTGRES_PASSWORD`
(owner). Generate passwords with `openssl rand -hex 24`. Never paste them into a
chat, ticket or shell history.

## First deploy (staging, then production)

1. VM exists, your SSH key works (H6). `git clone` the repo to the VM; check out a tag.
2. `sudo deploy/bootstrap-vm.sh` (re-runnable). Read its output; it warns if sshd
   was left unchanged.
3. Fill the two env files (above). `ls -l /etc/nimbus` must show `-rw-------`.
4. Build and start Postgres, then migrate and grant:
   ```sh
   docker compose --env-file /etc/nimbus/migrate.env -f docker-compose.yml build
   docker compose --env-file /etc/nimbus/migrate.env -f docker-compose.yml up -d postgres
   docker compose --env-file /etc/nimbus/migrate.env -f docker-compose.yml --profile ops run --rm migrate
   ```
   Expect `db-bootstrap ok: role nimbus_app grants applied`. Re-run `migrate`
   after EVERY upgrade (new tables have no app privileges until it runs).
5. `sudo systemctl start nimbus` (or `docker compose ... --profile app up -d`).
6. Verify (save output):
   ```sh
   docker compose -f docker-compose.yml --profile app ps          # app healthy
   docker compose -f docker-compose.yml logs app | grep "db role" # must say least-privilege
   docker compose -f docker-compose.yml exec app python -m src.app healthcheck
   docker inspect --format '{{.HostConfig.ReadonlyRootfs}} {{.Config.User}}' $(docker compose -f docker-compose.yml ps -q app)
   sudo ufw status verbose; ss -ltnp   # only 22 listening
   ```
   A `WARNING ... owner privileges` line means the app env file carries owner
   credentials or the app vars are missing: STOP, fix, do not go live.
7. Egress checks: `docs/runbooks/egress.md` "Verification".

## Cutover (from the previous bot / local run)

1. Freeze: stop the old process so two pollers never run (Telegram would 409).
2. Take a data snapshot if there is data to keep: `pg_dump` from the old DB
   (encrypted, off-box) and restore into the new Postgres before step 5 above
   (`pg_restore` as the OWNER, then re-run `migrate`).
3. Deploy as in "First deploy". Send one message; confirm a reply and that the
   heartbeat is fresh.
4. Watch `docker compose logs -f app` for 15 minutes; check spend cap, reminder
   delivery, no `WARNING` about owner privileges.
5. Only then retire the old host.

## Rollback

- App only: `git checkout <previous tag>`; `docker compose ... build`;
  `docker compose ... --profile app up -d app`. The offset/dedupe design makes
  restarts safe (at-least-once, deduped on `telegram:<update_id>`).
- Migrations are forward-only in practice: do NOT `alembic downgrade` in
  production. If a migration is bad: stop the app, restore the pre-deploy
  `pg_dump` into a fresh volume, deploy the previous tag.
- Take a `pg_dump` immediately before any upgrade that runs a migration.
- Kill switch without redeploy: the `system_flag` kill-switch row (see
  `src/llm`); stopping the app container is the blunt version.

## Rehearsal evidence (fill in on staging)

| Step | Date | Output saved at | Result |
|---|---|---|---|
| bootstrap-vm.sh | | | |
| migrate + db-bootstrap | | | |
| "db role ... least-privilege" log line | | | |
| egress verification | | | |
| rollback to previous tag | | | |

## Not covered here

Backups/restore drill, alerting (crash-loop, disk, backup age) and the Oracle
A1 retry script are separate P6 items. Host SSH/ufw/unattended-upgrades are in
the bootstrap script; fail2ban uses the Ubuntu default sshd jail.
