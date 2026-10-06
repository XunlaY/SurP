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
    assert "SurP" in response.text


async def test_static_dashboard_and_locales_are_not_cached(client):
    """The dashboard HTML and locale JSON must not be cached, otherwise a browser can
    keep serving a stale copy (e.g. missing a newly added Stop button/banner)."""
    ac, _, _ = client
    async with ac:
        index = await ac.get("/")
        en = await ac.get("/locales/en.json")
    assert index.headers.get("cache-control") == "no-store"
    assert en.headers.get("cache-control") == "no-store"


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
    state.decision = ControlDecision(
        enable_charging=True, target_amps=9, target_phases=1, reason="Test-Grund",
        reason_key="reason_charging_surplus", reason_vars={"w": "1500", "phases": "1", "amps": "9"},
    )
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
    assert body["decision"]["reason_key"] == "reason_charging_surplus"
    assert body["decision"]["reason_vars"] == {"w": "1500", "phases": "1", "amps": "9"}
    assert body["updated_at"] == now.isoformat()


async def test_status_serializes_wallbox_error_key(client):
    from piloading.models import CarState, WallboxStatus

    ac, state, _ = client
    now = datetime(2026, 6, 1, 12, 0, 0)
    state.wallbox = WallboxStatus(
        car_state=CarState.IDLE, charging=False, power_w=0.0, amps_effective=0, phases=1,
        energy_session_wh=0.0, error="RCCB ausgelöst", error_key="err_rccb", timestamp=now,
    )
    state.decision = ControlDecision(enable_charging=False, target_amps=6, target_phases=1, reason="r")

    async with ac:
        response = await ac.get("/api/status")

    body = response.json()
    assert body["wallbox"]["error"] == "RCCB ausgelöst"
    assert body["wallbox"]["error_key"] == "err_rccb"


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


def _lang_client(tmp_path, env_file):
    from piloading.controller import LatestState
    from piloading.history import HistoryStore
    from piloading.web.app import create_app

    history = HistoryStore(tmp_path / "history.db")
    state = LatestState()
    app = create_app(state, history, env_file=env_file)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_lang_defaults_to_en_when_no_env(tmp_path):
    env_file = tmp_path / ".env"
    ac = _lang_client(tmp_path, env_file)
    async with ac:
        response = await ac.get("/api/lang")
    assert response.status_code == 200
    assert response.json() == {"lang": "en"}


async def test_lang_get_reads_persisted_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PILOADING_MODE=live\nPILOADING_LANG=en\n", encoding="utf-8")
    ac = _lang_client(tmp_path, env_file)
    async with ac:
        response = await ac.get("/api/lang")
    assert response.status_code == 200
    assert response.json() == {"lang": "en"}


async def test_lang_post_persists_to_env_and_updates(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PILOADING_MODE=live\n", encoding="utf-8")
    ac = _lang_client(tmp_path, env_file)
    async with ac:
        response = await ac.post("/api/lang", json={"lang": "en"})
        assert response.status_code == 200
        assert response.json() == {"lang": "en"}

        # The running app reflects the new language immediately.
        follow_up = await ac.get("/api/lang")
        assert follow_up.json() == {"lang": "en"}

    # .env file now carries the persisted language.
    content = env_file.read_text(encoding="utf-8")
    assert "PILOADING_LANG=en" in content.splitlines()

    # A fresh app instance (simulating a restart) picks the persisted language up.
    ac2 = _lang_client(tmp_path, env_file)
    async with ac2:
        response = await ac2.get("/api/lang")
    assert response.json() == {"lang": "en"}


async def test_lang_post_overwrites_existing_key_in_place(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PILOADING_LANG=de\nPILOADING_MODE=live\n", encoding="utf-8")
    ac = _lang_client(tmp_path, env_file)
    async with ac:
        await ac.post("/api/lang", json={"lang": "en"})

    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines == ["PILOADING_LANG=en", "PILOADING_MODE=live"]


async def test_lang_post_rejects_unknown_language(tmp_path):
    env_file = tmp_path / ".env"
    ac = _lang_client(tmp_path, env_file)
    async with ac:
        response = await ac.post("/api/lang", json={"lang": "xx"})
    assert response.status_code == 400
    # Nothing was written to the env file.
    assert not env_file.exists() or "PILOADING_LANG" not in env_file.read_text()


async def test_locale_files_are_served(tmp_path):
    env_file = tmp_path / ".env"
    ac = _lang_client(tmp_path, env_file)
    async with ac:
        de = await ac.get("/locales/de.json")
        en = await ac.get("/locales/en.json")
    assert de.status_code == 200
    assert de.json()["night_banner"]
    assert en.status_code == 200
    assert en.json()["night_banner"]


async def test_status_reports_not_stopped_by_default(client):
    ac, _, _ = client
    async with ac:
        response = await ac.get("/api/status")
    assert response.json()["stopped"] is False


async def test_stop_and_continue_endpoints_toggle_logged_state(client):
    ac, state, _ = client
    async with ac:
        assert (await ac.get("/api/status")).json()["stopped"] is False

        r = await ac.post("/api/stop")
        assert r.status_code == 200
        assert r.json() == {"stopped": True}
        assert state.paused is True
        assert (await ac.get("/api/status")).json()["stopped"] is True

        r = await ac.post("/api/continue")
        assert r.status_code == 200
        assert r.json() == {"stopped": False}
        assert state.paused is False
        assert (await ac.get("/api/status")).json()["stopped"] is False


async def test_stop_endpoint_is_post_only_and_logs(client, caplog):
    """/api/stop must only accept POST (the dashboard now POSTs it) and log an INFO line
    so a stop is visible in the terminal. A GET — what the earlier frontend bug sent —
    must be rejected so this contract stays explicit."""
    ac, state, _ = client
    async with ac:
        # GET is not allowed: reject with 405 and do NOT toggle the state.
        r_get = await ac.get("/api/stop")
        assert r_get.status_code == 405
        assert state.paused is False

        # The real action is a POST and it logs at INFO.
        with caplog.at_level("INFO", logger="piloading.web.app"):
            r = await ac.post("/api/stop")
        assert r.status_code == 200
        assert state.paused is True
        assert any("gestoppt" in rec.getMessage() for rec in caplog.records)
        await ac.post("/api/continue")


async def test_dashboard_contains_stop_button(tmp_path):
    env_file = tmp_path / ".env"
    ac = _lang_client(tmp_path, env_file)
    async with ac:
        html = (await ac.get("/")).text
    assert 'id="stop-btn"' in html
    assert 'id="stop-banner"' in html
    # The Stop/Continue toggle must POST (the endpoints are POST-only); a GET here was
    # the original bug (405 Method Not Allowed).
    assert "postJSON(endpoint)" in html
    assert 'method: "POST"' in html
