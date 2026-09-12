# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Async Redis client for working memory."""

from redis.asyncio import Redis

from config.settings import settings

redis_client = Redis.from_url(settings.redis_url, decode_responses=True)


async def check_health() -> bool:
    try:
        return bool(await redis_client.ping())
    except Exception:  # noqa: BLE001 - health check must never raise
        return False
