"""FastAPI application entrypoint."""

from __future__ import annotations

from fastapi import FastAPI

from app.core import clock
from app.core.config import settings

app = FastAPI(title="Pinch Recovery Engine", version="0.1.0")


@app.get("/health")
def health() -> dict:
    """Liveness, plus the simulated-clock offset.

    The offset is here so a demo can confirm a fast-forward actually landed
    without reading the database.
    """
    return {
        "status": "ok",
        "pinch_mode": settings.PINCH_MODE,
        "clock_offset_seconds": clock.offset_seconds(),
    }
