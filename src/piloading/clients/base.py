"""Client interfaces. The controller and web layer depend only on these Protocols,
never on a concrete (real or mock) implementation — that's what lets the exact same
code run against real hardware on the Pi and against simulated data on a Mac.
"""

from __future__ import annotations

from typing import Protocol

from piloading.models import GridReading, WallboxStatus


class BatterySystemClient(Protocol):
    async def read(self) -> GridReading: ...


class WallboxClient(Protocol):
    async def read_status(self) -> WallboxStatus: ...

    async def set_charging(self, *, enabled: bool, amps: int, phases: int) -> None: ...
