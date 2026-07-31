"""Tests for the FastAPI dashboard against an in-memory ASGI transport — no real
server/socket needed."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from conftest import grid, wallbox
from piloading.controller import LatestState
from piloading.history import HistoryStore
from piloading.models import ControlDecision
from piloading.web.app import create_app


@pytest.fixture
def client(tmp_path):
    history = HistoryStore(tmp_path / "history.db")
    state = LatestState()
    app = create_app(state, history)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), state, history


async def test_health(client):
    ac, _, _ = client
    async with ac:
        response = await ac.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_index_serves_dashboard_html(client):
    ac, _, _ = client
    async with ac:
        response = await ac.get("/")
    assert response.status_code == 200
    assert "piLoading" in response.text


async def test_status_before_any_data_is_all_none(client):
    ac, _, _ = client
    async with ac:
        response = await ac.get("/api/status")
    body = response.json()
    assert body["grid"] is None
    assert body["wallbox"] is None
    assert body["decision"] is None
    assert body["stale"] is False


async def test_status_reflects_latest_state(client):
    ac, state, _ = client
    now = datetime(2026, 6, 1, 12, 0, 0)
    state.grid = grid(-1500, now)
    state.wallbox = wallbox(now, charging=True, power_w=2070, phases=1, amps=9)
    state.decision = ControlDecision(enable_charging=True, target_amps=9, target_phases=1, reason="Test-Grund")
    state.stale = True
    state.updated_at = now

    async with ac:
        response = await ac.get("/api/status")

    body = response.json()
    assert body["stale"] is True
    assert body["grid"]["grid_power_w"] == -1500
    assert body["wallbox"]["power_w"] == 2070
    assert body["wallbox"]["car_state"] == "CHARGING"
    assert body["decision"]["reason"] == "Test-Grund"
    assert body["updated_at"] == now.isoformat()


async def test_history_returns_inserted_samples(client):
    ac, _, history = client
    now = datetime.now()  # query_recent() filters against the real wall clock
    g = grid(-1000, now)
    wb = wallbox(now, charging=True, power_w=1500, phases=1, amps=6)
    decision = ControlDecision(enable_charging=True, target_amps=6, target_phases=1, reason="r")
    history.insert_sample(now, g, wb, decision)

    async with ac:
        response = await ac.get("/api/history", params={"hours": 24})

    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["grid_power_w"] == -1000
    assert rows[0]["wallbox_power_w"] == 1500
