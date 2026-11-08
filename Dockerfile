# syntax=docker/dockerfile:1.7
# Nimbus app image (P6.1). UNBUILT ON THE VM (H6); built and linted in CI only.
# Multi-arch: both bases below are manifest-list digests (linux/amd64 + linux/arm64),
# so `docker buildx build --platform linux/arm64` resolves the right layer.
# Refresh a digest:  docker buildx imagetools inspect python:3.12-slim-bookworm
#
# No secrets, no .env, no model weights are baked in (see .dockerignore). The
# local Qwen3 embedder (ADR-0013, extra `local-embed`) pulls torch (~GBs) and
# downloads weights at runtime; it is NOT in this image. Default APP_EMBEDDER=api.
# To opt in: build with --build-arg UV_EXTRAS="--extra local-embed" and mount a
# persistent HF cache volume (HF_HOME); document the size in the PR.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
ARG UV_EXTRAS=""
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
# Dependencies first (cache layer), frozen to uv.lock, no dev tools.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project ${UV_EXTRAS}

FROM ${PYTHON_IMAGE} AS runtime
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    APP_HEARTBEAT_PATH=/run/nimbus/heartbeat.json
# Fixed uid/gid so compose tmpfs/volume ownership can name them. No shell login.
RUN groupadd --system --gid 10001 nimbus \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin nimbus \
 && install -d -o root -g root -m 0755 /app
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=root:root alembic.ini ./
COPY --chown=root:root config ./config
COPY --chown=root:root migrations ./migrations
COPY --chown=root:root personality ./personality
COPY --chown=root:root src ./src
USER 10001:10001
# Liveness = fresh heartbeat file (ADR-0012). No DB, network or model call.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-m", "src.app", "healthcheck"]
ENTRYPOINT ["python", "-m", "src.app"]
CMD ["run"]
