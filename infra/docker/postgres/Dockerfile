# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

# Apache AGE ships pre-built on top of Postgres 16; add pgvector via apt.
FROM apache/age:release_PG16_1.5.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*

COPY init.sql /docker-entrypoint-initdb.d/10-init.sql
