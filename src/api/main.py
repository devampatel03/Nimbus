# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""FastAPI application entrypoint: lifespan, CORS, JWT auth middleware, routers.

Middleware order is load-bearing and non-obvious: Starlette's
`add_middleware` prepends, so the middleware added LAST ends up
OUTERMOST and runs FIRST on every request. `CORSMiddleware` must run
before `JWTAuthMiddleware` so a browser's `OPTIONS` preflight (which
never carries an `Authorization` header) is answered by CORS before an
auth check would reject it -- so `JWTAuthMiddleware` is added first,
`CORSMiddleware` last. `allow_credentials` stays False: auth here is a
Bearer token attached by client code, not a cookie, so credentialed CORS
was never needed, and it would otherwise conflict with the wildcard
`allow_origins` dev default (browsers reject `*` + credentials).

`app.state.orchestrator` / `app.state.tool_registry` are set synchronously in
`create_app` (no I/O), so the app is usable under test clients that never run
lifespan events. The default module-level `app` has NO orchestrator (chat
answers 503): building one needs the guarded `src.llm` module, an approved
character card and a policy, which the P2.1b entrypoint wires. This whole
package is deleted by P2.1b; it is kept minimal until then.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.settings import settings
from src.api import auth, health
from src.api.middleware.auth import JWTAuthMiddleware
from src.api.routes import chat, memory, tasks
from src.db import postgres
from src.memory.embedder import BatchEmbedder, EmbeddingMigrationRequired, Qwen3Embedder
from src.memory.manager import MemoryManager
from src.orchestrator.loop import Orchestrator
from src.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


async def verify_memory_index(embedder: BatchEmbedder | None = None) -> None:
    """P2.3/P2.4: refuse to start against an index built by another embedding
    model/dim (migration + re-embed job required). Only that condition is fatal;
    an unreachable database is reported by /health, not by crashing startup.
    Interim wiring: the P2.1b entrypoint replaces this lifespan."""
    try:
        async with postgres.get_session() as session:
            manager = MemoryManager(pg_session=session, embedder=embedder or Qwen3Embedder())
            await manager.verify_index()
    except EmbeddingMigrationRequired:
        raise
    except Exception:  # noqa: BLE001 - startup check is best-effort beyond the guard
        log.warning("memory index check skipped (database unavailable)")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await verify_memory_index(getattr(app.state, "memory_embedder", None))
    yield
    await postgres.engine.dispose()


def create_app(
    orchestrator: Orchestrator | None = None, registry: ToolRegistry | None = None
) -> FastAPI:
    app = FastAPI(title="Nimbus", lifespan=lifespan)
    app.state.orchestrator = orchestrator
    app.state.tool_registry = registry or ToolRegistry()

    # Order matters -- see module docstring.
    app.add_middleware(JWTAuthMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(auth.router, prefix="/auth", tags=["auth"])
    app.include_router(chat.router, prefix="/chat", tags=["chat"])
    app.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
    app.include_router(memory.router, prefix="/memories", tags=["memory"])
    return app


app = create_app()
