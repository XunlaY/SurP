"""Shared test fixtures and builders for constructing readings without hardware."""

from __future__ import annotations

from datetime import datetime

import pytest

from piloading.config import Settings
from piloading.models import CarState, GridReading, WallboxStatus


@pytest.fixture
def base_time() -> datetime:
    return datetime(2026, 6, 1, 12, 0, 0)


def make_settings(**overrides) -> Settings:
    defaults = dict(
        mode="mock",
        min_amp=6,
        max_amp=16,
        voltage=230.0,
        grid_power_smoothing_samples=1,
        target_grid_power_w=0.0,
        start_delay_s=90.0,
        stop_delay_s=90.0,
        phase_switch_sustain_s=300.0,
        phase_switch_min_interval_s=600.0,
        control_interval_s=10.0,
        enable_phase_switching=True,
        night_mode_after_s=600.0,
        night_mode_poll_interval_s=600.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def grid(power_w: float, ts: datetime) -> GridReading:
    return GridReading(
        grid_power_w=power_w,
        pv_power_w=None,
        battery_soc_pct=None,
        battery_power_w=None,
        timestamp=ts,
    )


def wallbox(ts: datetime, *, charging: bool, power_w: float = 0.0, phases: int = 1, amps: int = 0) -> WallboxStatus:
    return WallboxStatus(
        car_state=CarState.CHARGING if charging else CarState.WAIT_CAR,
        charging=charging,
        power_w=power_w,
        amps_effective=amps,
        phases=phases,
        energy_session_wh=0.0,
        error=None,
        timestamp=ts,
    )
