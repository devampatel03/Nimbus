# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Async Qdrant client for episodic vector memory."""

from qdrant_client import AsyncQdrantClient

from config.settings import settings

qdrant_client = AsyncQdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)


async def check_health() -> bool:
    try:
        await qdrant_client.get_collections()
        return True
    except Exception:  # noqa: BLE001 - health check must never raise
        return False
