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

`app.state.graph`/`app.state.graph_deps` are built synchronously in
`create_app`, not inside `lifespan`: building the graph does no I/O
(`GraphDependencies()`'s `ToolRegistry`/`Approver` are in-memory), so
there is no reason to defer it, and doing it eagerly means the app is
fully usable under test clients that never trigger ASGI lifespan events
(e.g. plain `httpx.ASGITransport`), not just under a real server.
`lifespan` is reserved for what actually needs a running loop tied to
the app's lifetime: closing the module-level DB clients at shutdown.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.settings import settings
from src.api import auth, health
from src.api.middleware.auth import JWTAuthMiddleware
from src.api.routes import chat, memory, tasks
from src.db import postgres
from src.orchestrator.graph import GraphDependencies, build_graph


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await postgres.engine.dispose()


def create_app(deps: GraphDependencies | None = None) -> FastAPI:
    app = FastAPI(title="Nimbus", lifespan=lifespan)

    deps = deps or GraphDependencies()
    app.state.graph_deps = deps
    app.state.graph = build_graph(deps)

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
