"""Mock clients used for `PILOADING_MODE=mock`: simulate a plausible solar day and a
charging car so the controller and dashboard can be exercised on a Mac with zero real
hardware. Both mocks implement the same Protocols (clients/base.py) as the real clients.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from piloading.models import CarState, GridReading, WallboxStatus


@dataclass
class MockClock:
    """Maps real elapsed wall-clock time to an accelerated simulated timestamp, so a
    full simulated day plays out in minutes instead of 24 hours.
    """

    speed_factor: float = 180.0  # 1 real second = 180 simulated seconds -> a day in ~8 real minutes
    start: datetime = field(default_factory=lambda: datetime.now().replace(hour=5, minute=0, second=0, microsecond=0))
    _real_start: float = field(default_factory=time.monotonic)

    def now(self) -> datetime:
        elapsed_real_s = time.monotonic() - self._real_start
        elapsed_sim_s = elapsed_real_s * self.speed_factor
        return self.start + timedelta(seconds=elapsed_sim_s)


def _random_walk(value: float, step: float, lo: float, hi: float) -> float:
    value += random.uniform(-step, step)
    return max(lo, min(hi, value))


def _solar_curve_w(now: datetime, peak_w: float, cloud_factor: float) -> float:
    hour = now.hour + now.minute / 60 + now.second / 3600
    if hour < 6 or hour > 20:
        return 0.0
    x = (hour - 13) / 7  # -1 at 06:00, 0 at 13:00, 1 at 20:00
    shape = max(0.0, math.cos(x * math.pi / 2)) ** 1.5
    return peak_w * shape * cloud_factor


class MockWallboxClient:
    """Simulates a go-e Wallbox with a car that's permanently plugged in."""

    def __init__(self, clock: MockClock, voltage: float = 230.0) -> None:
        self._clock = clock
        self._voltage = voltage
        self._plugged_in = True
        self._charging = False
        self._amps = 0
        self._phases = 1
        self._energy_session_wh = 0.0
        self._last_update = clock.now()

    @property
    def current_power_w(self) -> float:
        """Non-async accessor so MockBatterySystemClient can read live wallbox draw
        without going through the async Protocol method."""
        if not self._charging:
            return 0.0
        return self._amps * self._voltage * self._phases

    def _accumulate_energy(self, now: datetime) -> None:
        elapsed_h = (now - self._last_update).total_seconds() / 3600
        self._energy_session_wh += self.current_power_w * elapsed_h
        self._last_update = now

    async def read_status(self) -> WallboxStatus:
        now = self._clock.now()
        self._accumulate_energy(now)
        if not self._plugged_in:
            car_state = CarState.IDLE
        elif self._charging:
            car_state = CarState.CHARGING
        else:
            car_state = CarState.WAIT_CAR
        return WallboxStatus(
            car_state=car_state,
            charging=self._charging,
            power_w=self.current_power_w,
            amps_effective=self._amps if self._charging else 0,
            phases=self._phases,
            energy_session_wh=self._energy_session_wh,
            error=None,
            timestamp=now,
        )

    async def set_charging(self, *, enabled: bool, amps: int, phases: int) -> None:
        now = self._clock.now()
        self._accumulate_energy(now)
        if not self._plugged_in:
            self._charging = False
            self._amps = 0
            return
        self._charging = enabled
        self._phases = phases
        self._amps = amps if enabled else 0


class MockBatterySystemClient:
    """Simulates a Vitocharge-VX3-like PV + battery + grid-connection system, coupled
    to a MockWallboxClient so the wallbox's own draw is correctly reflected in the
    simulated grid balance (matching how a real grid meter would see it).
    """

    def __init__(
        self,
        clock: MockClock,
        wallbox: MockWallboxClient,
        *,
        peak_pv_w: float = 6000.0,
        household_base_w: float = 500.0,
        battery_capacity_wh: float = 10_000.0,
        battery_max_power_w: float = 4000.0,
        initial_soc_pct: float = 50.0,
    ) -> None:
        self._clock = clock
        self._wallbox = wallbox
        self._peak_pv_w = peak_pv_w
        self._household_base_w = household_base_w
        self._battery_capacity_wh = battery_capacity_wh
        self._battery_max_power_w = battery_max_power_w
        self._soc_pct = initial_soc_pct
        self._cloud_factor = 1.0
        self._load_factor = 0.0
        self._last_update = clock.now()

    def _battery_power_w(self, net_w: float, elapsed_h: float) -> float:
        """Positive = discharging, negative = charging. Respects SoC limits."""
        if net_w > 0:
            headroom_wh = self._battery_capacity_wh * (1 - self._soc_pct / 100)
            max_by_capacity_w = headroom_wh / elapsed_h if elapsed_h > 0 else self._battery_max_power_w
            charge_w = min(net_w, self._battery_max_power_w, max_by_capacity_w)
            return -charge_w
        if net_w < 0:
            available_wh = self._battery_capacity_wh * (self._soc_pct / 100)
            max_by_capacity_w = available_wh / elapsed_h if elapsed_h > 0 else self._battery_max_power_w
            discharge_w = min(-net_w, self._battery_max_power_w, max_by_capacity_w)
            return discharge_w
        return 0.0

    async def read(self) -> GridReading:
        now = self._clock.now()
        elapsed_h = max((now - self._last_update).total_seconds() / 3600, 1e-6)

        self._cloud_factor = _random_walk(self._cloud_factor, 0.05, 0.25, 1.0)
        self._load_factor = _random_walk(self._load_factor, 0.08, -1.0, 1.0)

        pv_w = _solar_curve_w(now, self._peak_pv_w, self._cloud_factor)
        household_w = max(0.0, self._household_base_w + self._load_factor * 300)
        total_load_w = household_w + self._wallbox.current_power_w

        net_w = pv_w - total_load_w
        battery_power_w = self._battery_power_w(net_w, elapsed_h)
        grid_power_w = -net_w - battery_power_w

        self._soc_pct += (-battery_power_w * elapsed_h / self._battery_capacity_wh) * 100
        self._soc_pct = max(0.0, min(100.0, self._soc_pct))
        self._last_update = now

        return GridReading(
            grid_power_w=grid_power_w,
            pv_power_w=pv_w,
            battery_soc_pct=self._soc_pct,
            battery_power_w=battery_power_w,
            timestamp=now,
        )
