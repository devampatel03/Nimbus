# 0001 — Plain Postgres 16 + pgvector; retire Apache AGE

- Status: Accepted (P0.4, 2026-10-24). Supersedes MEMORY.md AD-1.

## Context
AD-1 picked Postgres+AGE for graph queries. The audit (AUDIT_DISPOSITION §0.1) found that no code uses AGE. There is no Cypher and no `ag_catalog` use. Only `infra/docker/postgres/Dockerfile` (base `apache/age`) and `init.sql` reference it. The bi-temporal graph is ordinary relational tables (`graph_node`, `graph_edge`). pgvector is installed but no vector column exists (§0.2). Episodic vectors live in Qdrant, which the Brief removes. AGE's arm64 image availability was never verified for the Ampere A1 target.

## Decision
- Use `pgvector/pgvector:pg16`. Its multi-arch amd64+arm64 manifest was verified on Docker Hub, 2026-10-24.
- Drop AGE from the image and from `init.sql`.
- Keep `graph_node`/`graph_edge` unchanged. Add `memory_chunk` with `embedding vector(1024)`, `model_id`, `dim`, provenance and `untrusted`, plus an HNSW cosine index.
- If multi-hop graph traversal is ever needed, use recursive CTEs.

## Consequences
- One fewer extension to patch or back up. `pg_dump` restore is standard (R14).
- Dev volumes holding the `ag_catalog` schema must be recreated. No production data exists.
- No Cypher. Graph-shaped questions get answered with SQL joins or CTEs, which is enough for a single user's fact volume.
- Changing the embedding dimension means a re-embed migration. `model_id` and `dim` are stored per row so the mismatch can be detected.

## Alternatives
- **Keep AGE.** Rejected: it is unused, arm64 is unverified, and it adds an extension upgrade burden.
- **Separate vector DB (Qdrant).** Rejected: it is a second stateful service with its own backups. The Brief mandates pgvector.
- **SQLite + sqlite-vec.** Rejected: the Brief fixes Postgres, and the existing ORM, migrations and tests are Postgres-specific.
