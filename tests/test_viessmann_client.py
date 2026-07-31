"""Tests for ViessmannClient against a mocked HTTP layer — no real account needed."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from piloading.clients.viessmann import ViessmannClient

TOKEN_URL = "https://iam.viessmann-climatesolutions.com/idp/v3/token"
FEATURES_URL = (
    "https://api.viessmann-climatesolutions.com/iot/v2/features/installations/12345"
    "/gateways/7736571234567890/devices/0/features"
)

AUTH_DATA = {
    "client_id": "test-client-id",
    "access_token": "old-access-token",
    "refresh_token": "old-refresh-token",
    "installation_id": "12345",
    "gateway_serial": "7736571234567890",
    "device_id": "0",
}


def _auth_file(tmp_path):
    path = tmp_path / "viessmann_auth.json"
    path.write_text(json.dumps(AUTH_DATA))
    return path


def _feature(name: str, value: float) -> dict:
    return {"feature": name, "properties": {"value": {"type": "number", "value": value, "unit": "watt"}}}


def _mock_token_refresh(new_access="new-access-token", new_refresh="new-refresh-token", status=200):
    return respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            status,
            json={"access_token": new_access, "refresh_token": new_refresh, "expires_in": 3600}
            if status < 400
            else {"error": "invalid_grant"},
        )
    )


@respx.mock
async def test_read_parses_grid_pv_battery_and_refreshes_token_first(tmp_path):
    token_route = _mock_token_refresh()
    respx.get(FEATURES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _feature("pcc.transfer.power.exchange", 450.0),
                    _feature("photovoltaic.production.current", 3200.0),
                    _feature("ess.power", -1500.0),
                    _feature("ess.stateOfCharge", 72.0),
                ]
            },
        )
    )
    client = ViessmannClient(_auth_file(tmp_path))

    reading = await client.read()

    assert token_route.called
    assert reading.grid_power_w == pytest.approx(450.0)
    assert reading.pv_power_w == pytest.approx(3200.0)
    assert reading.battery_power_w == pytest.approx(-1500.0)
    assert reading.battery_soc_pct == pytest.approx(72.0)

    saved = json.loads((tmp_path / "viessmann_auth.json").read_text())
    assert saved["access_token"] == "new-access-token"


@respx.mock
async def test_invert_grid_power_sign_flips_sign(tmp_path):
    _mock_token_refresh()
    respx.get(FEATURES_URL).mock(
        return_value=httpx.Response(200, json={"data": [_feature("pcc.transfer.power.exchange", 450.0)]})
    )
    client = ViessmannClient(_auth_file(tmp_path), invert_grid_power_sign=True)

    reading = await client.read()

    assert reading.grid_power_w == pytest.approx(-450.0)


@respx.mock
async def test_missing_optional_features_return_none(tmp_path):
    _mock_token_refresh()
    respx.get(FEATURES_URL).mock(
        return_value=httpx.Response(200, json={"data": [_feature("pcc.transfer.power.exchange", 100.0)]})
    )
    client = ViessmannClient(_auth_file(tmp_path))

    reading = await client.read()

    assert reading.grid_power_w == pytest.approx(100.0)
    assert reading.pv_power_w is None
    assert reading.battery_power_w is None
    assert reading.battery_soc_pct is None


@respx.mock
async def test_read_returns_cached_reading_on_transient_failure(tmp_path):
    _mock_token_refresh()
    features_route = respx.get(FEATURES_URL).mock(
        return_value=httpx.Response(200, json={"data": [_feature("pcc.transfer.power.exchange", 200.0)]})
    )
    client = ViessmannClient(_auth_file(tmp_path))
    first = await client.read()
    assert first.grid_power_w == pytest.approx(200.0)

    features_route.mock(return_value=httpx.Response(500))
    second = await client.read()

    assert second is first  # served from cache, not a fresh (failed) reading


@respx.mock
async def test_raises_when_refresh_fails_and_no_cache_yet(tmp_path):
    _mock_token_refresh(status=400)
    client = ViessmannClient(_auth_file(tmp_path))

    with pytest.raises(RuntimeError):
        await client.read()
