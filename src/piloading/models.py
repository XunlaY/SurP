"""Shared data types passed between clients, the controller, and the web layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class CarState(Enum):
    """Mirrors go-e local API v1's `car` field (see clients/goe.py). ERROR is a defensive
    extra never actually produced by the v1 API, which reports hardware errors via the
    separate `err` key instead.
    """

    UNKNOWN = 0
    IDLE = 1
    CHARGING = 2
    WAIT_CAR = 3
    COMPLETE = 4
    ERROR = 5


@dataclass(frozen=True, slots=True)
class GridReading:
    """A snapshot of the household's power balance, as read from the battery/inverter system.

    grid_power_w: positive = importing from the grid, negative = exporting (surplus).
    """

    grid_power_w: float
    pv_power_w: float | None
    battery_soc_pct: float | None
    battery_power_w: float | None
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class WallboxStatus:
    car_state: CarState
    charging: bool
    power_w: float
    amps_effective: int
    phases: int
    energy_session_wh: float
    error: str | None
    timestamp: datetime
    # Catalog key (see web/static/locales/*.json) for `error`, so the dashboard can
    # render it in the selected language. None when there is no error or it has no
    # localizable translation (e.g. the free-form fallback message).
    error_key: str | None = None


@dataclass(frozen=True, slots=True)
class ControlDecision:
    enable_charging: bool
    target_amps: int
    target_phases: int
    reason: str
    # True once a sustained deficit has triggered night mode (see SurplusController.decide
    # and Settings.night_mode_after_s) — tells run_control_loop to poll at
    # night_mode_poll_interval_s instead of control_interval_s.
    night_mode: bool = False
    # Structured, language-neutral form of `reason` for the dashboard: a catalog key
    # (see web/static/locales/*.json) plus its interpolation variables. The dashboard
    # prefers these to render `reason` in the user's selected language; `reason` itself
    # stays as the German human-readable text for logs and history. None when the reason
    # is not localizable (e.g. a free-form decision).
    reason_key: str | None = None
    reason_vars: dict[str, str | int | float] | None = None
