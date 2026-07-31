"""FastAPI dashboard: one static HTML page plus a small JSON API reading the
in-memory LatestState the control loop updates every cycle. No auth — LAN-only."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse

from piloading.controller import LatestState
from piloading.history import HistoryStore
from piloading.models import GridReading, WallboxStatus

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(state: LatestState, history: HistoryStore) -> FastAPI:
    app = FastAPI(title="piLoading")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return {
            "updated_at": state.updated_at.isoformat() if state.updated_at else None,
            "stale": state.stale,
            "grid": _serialize_grid(state.grid) if state.grid else None,
            "wallbox": _serialize_wallbox(state.wallbox) if state.wallbox else None,
            "decision": asdict(state.decision) if state.decision else None,
        }

    @app.get("/api/history")
    async def history_endpoint(hours: float = 24.0) -> list[dict[str, Any]]:
        return history.query_recent(hours)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    return app


def _serialize_grid(grid: GridReading) -> dict[str, Any]:
    d = asdict(grid)
    d["timestamp"] = grid.timestamp.isoformat()
    return d


def _serialize_wallbox(wallbox: WallboxStatus) -> dict[str, Any]:
    d = asdict(wallbox)
    d["timestamp"] = wallbox.timestamp.isoformat()
    d["car_state"] = wallbox.car_state.name
    return d
