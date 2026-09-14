"""Liveness endpoint.

Intentionally does not call GitHub: a health check that depends on a third
party will flap when that third party has a bad minute, and container
orchestrators will restart a perfectly healthy process.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.config import Settings
from app.dependencies import get_settings_dep
from app.models import HealthResponse

router = APIRouter(tags=["meta"])

VERSION = "1.0.0"


@router.get("/healthz", response_model=HealthResponse, summary="Liveness probe")
async def healthz(settings: Annotated[Settings, Depends(get_settings_dep)]) -> HealthResponse:
    return HealthResponse(status="ok", repo=settings.repo_path, version=VERSION)
