# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Health check endpoint reporting status of all backing stores."""

from fastapi import APIRouter

from src.db import postgres

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, object]:
    postgres_ok = await postgres.check_health()

    stores = {
        "postgres": "ok" if postgres_ok else "unavailable",
    }
    overall = "ok" if postgres_ok else "degraded"

    return {"status": overall, "stores": stores}
