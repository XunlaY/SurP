"""FastAPI dashboard: one static HTML page plus a small JSON API reading the
in-memory LatestState the control loop updates every cycle. No auth — LAN-only."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from piloading.controller import LatestState
from piloading.history import HistoryStore
from piloading.models import GridReading, WallboxStatus

_STATIC_DIR = Path(__file__).parent / "static"
_LOCALES_DIR = _STATIC_DIR / "locales"

_ENV_LANG_VAR = "PILOADING_LANG"


def _available_langs() -> set[str]:
    return {p.stem for p in _LOCALES_DIR.glob("*.json")}


def _load_current_lang(env_file: Path) -> str:
    """Read the persisted PILOADING_LANG from the .env file, if present and valid."""
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(_ENV_LANG_VAR + "="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value in _available_langs():
                    return value
    except FileNotFoundError:
        pass
    return "en"


def _persist_lang(env_file: Path, lang: str) -> None:
    """Write/overwrite PILOADING_LANG in the .env file so it survives a restart.

    Preserves all other lines and the existing key's position, or appends a new line
    (with a leading comment block) if the key is not present yet.
    """
    if lang not in _available_langs():
        raise ValueError(f"Unknown language: {lang}")

    if not env_file.exists():
        env_file.touch()

    lines = env_file.read_text(encoding="utf-8").splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith(_ENV_LANG_VAR + "="):
            lines[i] = f"{_ENV_LANG_VAR}={lang}"
            replaced = True
            break

    if not replaced:
        lines.append("")
        lines.append("# Dashboard display language (ISO 639-1). Set via the language")
        lines.append("# button in the dashboard; persisted here so it survives a restart.")
        lines.append(f"{_ENV_LANG_VAR}={lang}")

    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_app(
    state: LatestState,
    history: HistoryStore,
    env_file: Path = Path(".env"),
) -> FastAPI:
    app = FastAPI(title="piLoading")
    current_lang = _load_current_lang(env_file)

    # Serve the locale JSON files (de.json, en.json, ...) so the dashboard can load
    # its strings over HTTP. ~/.env is gitignored, so only these JSON files ship.
    app.mount("/locales", StaticFiles(directory=_LOCALES_DIR), name="locales")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/lang")
    async def lang_get() -> dict[str, str]:
        return {"lang": current_lang}

    @app.post("/api/lang")
    async def lang_set(payload: dict[str, str]) -> dict[str, str]:
        nonlocal current_lang
        lang = (payload.get("lang") or "").strip()
        if lang not in _available_langs():
            raise HTTPException(status_code=400, detail=f"Unknown language: {lang}")
        _persist_lang(env_file, lang)
        current_lang = lang
        return {"lang": current_lang}

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
