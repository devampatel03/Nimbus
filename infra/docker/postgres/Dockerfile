# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

# Official pgvector image on Postgres 16 (multi-arch: linux/amd64 + linux/arm64).
# ADR 0001: plain Postgres + pgvector; Apache AGE retired.
FROM pgvector/pgvector:pg16

COPY init.sql /docker-entrypoint-initdb.d/10-init.sql
