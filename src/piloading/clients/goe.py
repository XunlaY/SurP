"""Real go-e Wallbox client — local HTTP API v1 (the classic filter/`nrg`-array API;
still the default and most broadly supported local API across go-e hardware
generations: https://github.com/goecharger/go-eCharger-API-v1).

Key choices, corrected against a live device (the original plan guessed a few key
names that turned out not to exist on this firmware at all):
  - `amx` does not exist on this hardware — writing it returns HTTP 500. Likewise
    `alw` is read-only here (writing it 500s with `"alw: tried to set api key
    without setter"`); it only ever reflects derived charge-allowed status. Both
    were classic API v1 keys; this charger's firmware speaks the v2-native write
    model regardless of which "API version" toggle is enabled in its settings.
  - Current is written via `amp` and enable/disable via `frc` (forceState:
    Neutral=0, Off=1, On=2 — On/Off are used here, never Neutral, since Neutral
    hands control back to the charger's own internal logic). Both are confirmed
    live and match the official v2 key reference
    (goecharger/go-eCharger-API-v2, API_KEYS_FIRMWARE/apikeys-en.md).
  - Unlike the old `amx` (RAM-only, safe to write every cycle), `amp`/`frc` are
    `Config`-category keys persisted to flash, rated for ~100k write cycles per
    go-e's own docs. `set_charging` therefore only issues a write when the
    target actually changed since the last call — same pattern already used
    below for `psm` — to avoid wearing out the EEPROM from a tight control loop.
  - Phase switching (`psm`) is NOT part of the official spec at all. It is
    confirmed only by independent community reverse-engineering (go-eCharger-API-v2
    issue #58, various forum reports), which agree on psm=1 (1-phase) / psm=2
    (3-phase) — NOT 0/1/3. Treat this as best-effort and verify empirically: the
    `pha` read-back (officially documented) is what actually confirms whether a
    switch took effect, so `read_status()` always derives `phases` from `pha`,
    never from what we last requested.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from piloading.models import CarState, WallboxStatus

logger = logging.getLogger(__name__)

_STATUS_FILTER = "car,alw,amp,ama,err,pha,nrg,dws,eto"

_ERROR_MESSAGES = {
    1: "RCCB ausgelöst",
    3: "Phasenstörung",
    8: "Erdung fehlt (NO_GROUND)",
    10: "Interner Fehler",
}

# Undocumented, community-reported values only — see module docstring.
_PSM_ONE_PHASE = 1
_PSM_THREE_PHASE = 2

# Official v2 forceState values (apikeys-en.md) — Neutral (0) is intentionally unused,
# see module docstring.
_FRC_OFF = 1
_FRC_ON = 2


class GoeWallboxClient:
    def __init__(self, host: str, *, timeout_s: float = 5.0) -> None:
        self._client = httpx.AsyncClient(base_url=f"http://{host}", timeout=timeout_s)
        self._warned_ama = False
        self._last_written_phases: int | None = None
        self._last_written_amps: int | None = None
        self._last_written_enabled: bool | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def read_status(self) -> WallboxStatus:
        response = await self._client.get("/api/status", params={"filter": _STATUS_FILTER})
        response.raise_for_status()
        data = response.json()

        car_raw = int(data.get("car", 0))
        try:
            car_state = CarState(car_raw)
        except ValueError:
            logger.warning("Unbekannter car-Status vom go-e: %r", car_raw)
            car_state = CarState.UNKNOWN
        charging = car_state is CarState.CHARGING

        nrg = data.get("nrg") or [0] * 16
        power_w = nrg[11] * 10.0  # nrg[11] is total power in 0.01kW units
        amps_effective = round(max(nrg[4:7]) / 10.0) if charging else 0  # nrg[4:7] is per-phase current in 0.1A

        pha_raw = data.get("pha", 0)
        if isinstance(pha_raw, list):
            # Some go-e firmwares send pha as a 6-element bool array instead of the
            # documented bitmask int: [phase3_before, phase2_before, phase1_before,
            # phase3_after, phase2_after, phase1_after] — same bit order as the
            # 0b00ABCDEF bitmask, confirmed empirically against a live device.
            phases_after_contactor = sum(1 for p in pha_raw[3:6] if p)
        else:
            phases_after_contactor = bin(int(pha_raw) & 0b111).count("1")
        phases = phases_after_contactor or 1

        err_raw = int(data.get("err", 0))
        error = _ERROR_MESSAGES.get(err_raw, f"Unbekannter Fehlercode {err_raw}") if err_raw else None

        dws = float(data.get("dws", 0))  # deca-watt-seconds
        energy_session_wh = dws * 10.0 / 3600.0

        ama = data.get("ama")
        if ama is not None and not self._warned_ama:
            self._warned_ama = True
            logger.info("go-e meldet absolutes Strom-Maximum (ama) = %sA", ama)

        return WallboxStatus(
            car_state=car_state,
            charging=charging,
            power_w=power_w,
            amps_effective=amps_effective,
            phases=phases,
            energy_session_wh=energy_session_wh,
            error=error,
            timestamp=datetime.now(),
        )

    async def set_charging(self, *, enabled: bool, amps: int, phases: int) -> None:
        params = {}
        # amp/frc/psm are all flash-backed Config keys on this hardware (~100k write
        # cycles), unlike the old RAM-only amx — only send a key when its value actually
        # changed since the last call, to avoid wearing out the EEPROM every control cycle.
        if amps != self._last_written_amps:
            params["amp"] = str(amps)
        if enabled != self._last_written_enabled:
            params["frc"] = str(_FRC_ON if enabled else _FRC_OFF)
        if phases != self._last_written_phases:
            params["psm"] = str(_PSM_THREE_PHASE if phases == 3 else _PSM_ONE_PHASE)

        if not params:
            return

        response = await self._client.get("/api/set", params=params)
        response.raise_for_status()
        self._last_written_amps = amps
        self._last_written_enabled = enabled
        self._last_written_phases = phases
