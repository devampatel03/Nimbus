# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Unit tests for the /health endpoint, with all stores mocked."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.health import router


def make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_health_all_stores_ok():
    with (
        patch("src.api.health.postgres.check_health", new=AsyncMock(return_value=True)),
    ):
        transport = ASGITransport(app=make_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "ok",
        "stores": {"postgres": "ok"},
    }


@pytest.mark.asyncio
async def test_health_one_store_down_reports_degraded():
    with (
        patch("src.api.health.postgres.check_health", new=AsyncMock(return_value=False)),
    ):
        transport = ASGITransport(app=make_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["stores"]["postgres"] == "unavailable"
