"""Real Viessmann Vitocharge VX3 client — polls grid/PV/battery power via the cloud
IoT API. Requires a token file created once via `piloading auth-viessmann`
(see viessmann_auth.py).

Confidence levels on the feature names below (see the plan's "known risks" section):
  - `pcc.transfer.power.exchange` (grid power at the point of common coupling) is
    corroborated by a real Home Assistant integration report for a comparable
    VX3 + energy-meter setup. Sign convention is still unconfirmed — that's what
    `invert_grid_power_sign` is for; verify empirically against a known-import vs.
    known-export moment and flip the config flag if the sign is backwards.
  - PV/battery feature names are educated guesses, not confirmed for this exact
    hardware. Rather than hardcode one exact path per reading (which fails silently
    if the guess is wrong), this client fetches the FULL feature list every poll and
    searches it by name prefix, and logs the complete discovered feature list once at
    startup — so a wrong guess is immediately visible in the log and a one-line fix,
    not a mystery. PV/battery readings are optional (None if not found); only grid
    power is required for the control loop to function.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from piloading.models import GridReading

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://iam.viessmann-climatesolutions.com/idp/v3/token"
_API_BASE = "https://api.viessmann-climatesolutions.com"
_TOKEN_REFRESH_MARGIN_S = 600.0  # refresh 10 min before expiry, not right at the deadline

_GRID_FEATURE = "pcc.transfer.power.exchange"
_PV_FEATURE_PREFIXES = ("photovoltaic.production.current", "photovoltaic.power")
_BATTERY_POWER_PREFIXES = ("ess.power",)
_BATTERY_SOC_PREFIXES = ("ess.stateOfCharge",)


class ViessmannAuthError(RuntimeError):
    """The stored token can't be refreshed — needs `piloading auth-viessmann` again."""


class ViessmannClient:
    def __init__(self, auth_file: Path, *, invert_grid_power_sign: bool = False, timeout_s: float = 15.0) -> None:
        self._auth_file = auth_file
        self._invert_sign = invert_grid_power_sign
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._auth: dict[str, Any] = json.loads(auth_file.read_text())
        self._access_token_expires_at = 0.0  # force a refresh before the first request
        self._last_reading: GridReading | None = None
        self._logged_features_once = False

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _ensure_access_token(self) -> str:
        if time.monotonic() < self._access_token_expires_at:
            return self._auth["access_token"]

        response = await self._client.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self._auth["client_id"],
                "refresh_token": self._auth["refresh_token"],
            },
        )
        if response.status_code >= 400:
            raise ViessmannAuthError(
                f"Token-Refresh fehlgeschlagen (HTTP {response.status_code}). "
                "Bitte `piloading auth-viessmann` erneut ausführen."
            )
        payload = response.json()
        self._auth["access_token"] = payload["access_token"]
        self._auth["refresh_token"] = payload.get("refresh_token", self._auth["refresh_token"])
        self._access_token_expires_at = (
            time.monotonic() + payload.get("expires_in", 3600) - _TOKEN_REFRESH_MARGIN_S
        )
        self._auth_file.write_text(json.dumps(self._auth, indent=2))
        self._auth_file.chmod(0o600)
        return self._auth["access_token"]

    async def _fetch_features(self) -> list[dict[str, Any]]:
        token = await self._ensure_access_token()
        url = (
            f"{_API_BASE}/iot/v2/features/installations/{self._auth['installation_id']}"
            f"/gateways/{self._auth['gateway_serial']}/devices/{self._auth['device_id']}/features"
        )
        response = await self._client.get(url, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        features: list[dict[str, Any]] = response.json().get("data", [])

        if not self._logged_features_once:
            self._logged_features_once = True
            names = sorted(f.get("feature", "?") for f in features)
            logger.info("Verfügbare Viessmann-Features (%d): %s", len(names), names)

        return features

    @staticmethod
    def _find_value(features: list[dict[str, Any]], *, exact: str | None = None, prefixes: tuple[str, ...] = ()) -> float | None:
        for feature in features:
            name = feature.get("feature", "")
            if name == exact or (prefixes and name.startswith(prefixes)):
                value = feature.get("properties", {}).get("value", {}).get("value")
                if value is None:
                    logger.warning("Feature '%s' gefunden, aber keine properties.value.value: %r", name, feature)
                    return None
                return float(value)
        return None

    async def read(self) -> GridReading:
        try:
            features = await self._fetch_features()

            grid_raw = self._find_value(features, exact=_GRID_FEATURE)
            if grid_raw is None:
                raise RuntimeError(f"Feature '{_GRID_FEATURE}' nicht in Viessmann-Antwort gefunden")
            grid_power_w = -grid_raw if self._invert_sign else grid_raw

            reading = GridReading(
                grid_power_w=grid_power_w,
                pv_power_w=self._find_value(features, prefixes=_PV_FEATURE_PREFIXES),
                battery_soc_pct=self._find_value(features, prefixes=_BATTERY_SOC_PREFIXES),
                battery_power_w=self._find_value(features, prefixes=_BATTERY_POWER_PREFIXES),
                timestamp=datetime.now(),
            )
            self._last_reading = reading
            return reading

        except ViessmannAuthError:
            logger.exception("Viessmann-Authentifizierung ungültig")
        except Exception:
            logger.exception("Viessmann-Abfrage fehlgeschlagen")

        if self._last_reading is not None:
            return self._last_reading
        raise RuntimeError("Noch kein erfolgreicher Viessmann-Read und aktueller Read fehlgeschlagen")
