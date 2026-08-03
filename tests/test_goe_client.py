"""Tests for GoeWallboxClient against a mocked HTTP layer — no real wallbox needed."""

from __future__ import annotations

import httpx
import pytest
import respx

from piloading.clients.goe import GoeWallboxClient
from piloading.models import CarState

HOST = "192.168.1.50"


def _status_json(**overrides):
    body = {
        "car": "2",
        "alw": "1",
        "amp": "16",
        "ama": "32",
        "err": "0",
        "pha": "63",  # 0b00111111: all 3 phases before AND after the contactor
        "nrg": [230, 231, 229, 0, 160, 160, 160, 36, 37, 36, 0, 11040, 98, 98, 98, 0],
        "dws": "12345",
        "eto": "500",
    }
    body.update(overrides)
    return body


@respx.mock
async def test_read_status_parses_realistic_response():
    respx.get(f"http://{HOST}/api/status").mock(return_value=httpx.Response(200, json=_status_json()))
    client = GoeWallboxClient(HOST)

    status = await client.read_status()

    assert status.car_state is CarState.CHARGING
    assert status.charging is True
    assert status.power_w == pytest.approx(11040.0)  # nrg[11] is plain Watts on this firmware
    assert status.amps_effective == 16  # max(nrg[4:7]) / 10
    assert status.phases == 3  # pha=63 -> 3 bits set after the contactor
    assert status.energy_session_wh == pytest.approx(34.2917, rel=1e-3)  # dws=12345 * 10 / 3600
    assert status.error is None


@respx.mock
async def test_read_status_parses_pha_as_bool_array():
    # Some go-e firmwares send pha as [phase3_before, phase2_before, phase1_before,
    # phase3_after, phase2_after, phase1_after] instead of the documented bitmask int.
    respx.get(f"http://{HOST}/api/status").mock(
        return_value=httpx.Response(200, json=_status_json(pha=[False, False, False, True, True, True]))
    )
    client = GoeWallboxClient(HOST)

    status = await client.read_status()

    assert status.phases == 3


@respx.mock
async def test_read_status_maps_known_error_code_and_zeroes_amps_when_idle():
    respx.get(f"http://{HOST}/api/status").mock(
        return_value=httpx.Response(200, json=_status_json(car="1", err="1"))
    )
    client = GoeWallboxClient(HOST)

    status = await client.read_status()

    assert status.car_state is CarState.IDLE
    assert status.charging is False
    assert status.amps_effective == 0  # not charging -> ignore the live current reading
    assert status.error == "RCCB ausgelöst"


@respx.mock
async def test_set_charging_sends_amp_frc_and_psm_on_first_call():
    route = respx.get(f"http://{HOST}/api/set").mock(return_value=httpx.Response(200, json={}))
    client = GoeWallboxClient(HOST)

    await client.set_charging(enabled=True, amps=10, phases=3)

    request = route.calls.last.request
    assert request.url.params["amp"] == "10"
    assert request.url.params["frc"] == "2"  # forceState On
    assert request.url.params["psm"] == "2"  # community-reported value for 3-phase


@respx.mock
async def test_set_charging_omits_psm_when_phase_unchanged_but_resends_on_switch():
    route = respx.get(f"http://{HOST}/api/set").mock(return_value=httpx.Response(200, json={}))
    client = GoeWallboxClient(HOST)

    await client.set_charging(enabled=True, amps=10, phases=3)
    await client.set_charging(enabled=True, amps=12, phases=3)

    unchanged_request = route.calls.last.request
    assert unchanged_request.url.params["amp"] == "12"
    assert "frc" not in unchanged_request.url.params  # enabled didn't change
    assert "psm" not in unchanged_request.url.params

    await client.set_charging(enabled=True, amps=12, phases=1)
    switched_request = route.calls.last.request
    assert switched_request.url.params["psm"] == "1"  # community-reported value for 1-phase


@respx.mock
async def test_set_charging_skips_request_entirely_when_nothing_changed():
    route = respx.get(f"http://{HOST}/api/set").mock(return_value=httpx.Response(200, json={}))
    client = GoeWallboxClient(HOST)

    await client.set_charging(enabled=True, amps=10, phases=3)
    assert route.call_count == 1

    # Identical target on the next control cycle must not hit the flash-backed keys again.
    await client.set_charging(enabled=True, amps=10, phases=3)
    assert route.call_count == 1


@respx.mock
async def test_set_charging_maps_disabled_to_frc_off():
    route = respx.get(f"http://{HOST}/api/set").mock(return_value=httpx.Response(200, json={}))
    client = GoeWallboxClient(HOST)

    await client.set_charging(enabled=False, amps=6, phases=1)

    request = route.calls.last.request
    assert request.url.params["frc"] == "1"  # forceState Off
