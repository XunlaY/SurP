"""Surplus-charging decision logic (pure, unit-testable) plus the async orchestration
loop that wires it to the real/mock clients, history storage, and shared web state.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from piloading.clients.base import BatterySystemClient, WallboxClient
from piloading.config import Settings
from piloading.history import HistoryStore
from piloading.models import CarState, ControlDecision, GridReading, WallboxStatus

# car_state values for which no charging session can be in progress: nothing for the
# controller to decide, so the control loop skips the Viessmann read entirely and only
# keeps polling go-e (at the slower goe_poll_interval_s) to notice a car being plugged
# back in or a new session starting.
_NO_SESSION_STATES = (CarState.IDLE, CarState.COMPLETE)

logger = logging.getLogger(__name__)


class SurplusController:
    """Pure decision logic: given readings + a clock, decide how the wallbox should be
    driven. No I/O, no `await` — this is what makes it unit-testable by feeding a
    synthetic sequence of readings and clock values, independent of any client.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._grid_history: deque[float] = deque(maxlen=settings.grid_power_smoothing_samples)
        self._charging = False
        self._phases = 1
        self._last_amps = settings.min_amp
        self._surplus_since: datetime | None = None
        self._deficit_since: datetime | None = None
        self._phase_crossing_since: datetime | None = None
        self._phase_crossing_wants: int | None = None
        self._last_phase_switch: datetime | None = None
        self._last_amp_change: datetime | None = None

    def decide(self, grid: GridReading, wallbox: WallboxStatus, now: datetime) -> ControlDecision:
        s = self._settings
        self._grid_history.append(grid.grid_power_w)
        smoothed = sum(self._grid_history) / len(self._grid_history)

        current_wallbox_power = wallbox.power_w if wallbox.charging else 0.0
        target_power = current_wallbox_power - (smoothed - s.target_grid_power_w)

        self._update_phase_decision(target_power, now)

        sufficient_w = s.min_amp * s.voltage * self._phases
        if target_power >= sufficient_w:
            self._deficit_since = None
            if self._surplus_since is None:
                self._surplus_since = now
            if not self._charging and now - self._surplus_since >= timedelta(seconds=s.start_delay_s):
                self._charging = True
            if self._charging:
                raw_amps = target_power / (s.voltage * self._phases)
                new_amps = max(s.min_amp, min(s.max_amp, round(raw_amps)))
                cooled_down = self._last_amp_change is None or (
                    now - self._last_amp_change >= timedelta(seconds=s.amp_change_cooldown_s)
                )
                if new_amps != self._last_amps and cooled_down:
                    self._last_amps = new_amps
                    self._last_amp_change = now
            reason = (
                f"Überschuss reicht ({target_power:.0f} W) – lade {self._phases}-phasig mit {self._last_amps} A"
                if self._charging
                else f"Überschuss seit {self._elapsed(self._surplus_since, now):.0f}s, "
                f"warte auf Startschwelle ({s.start_delay_s:.0f}s)"
            )
        else:
            self._surplus_since = None
            if self._deficit_since is None:
                self._deficit_since = now
            if self._charging and now - self._deficit_since >= timedelta(seconds=s.stop_delay_s):
                self._charging = False
            reason = (
                f"Kein ausreichender Überschuss seit {self._elapsed(self._deficit_since, now):.0f}s"
                if not self._charging
                else f"Überschuss reicht nicht mehr, Stoppschwelle in "
                f"{max(0.0, s.stop_delay_s - self._elapsed(self._deficit_since, now)):.0f}s (halte {self._last_amps} A)"
            )

        return ControlDecision(
            enable_charging=self._charging,
            target_amps=self._last_amps,
            target_phases=self._phases,
            reason=reason,
        )

    def _update_phase_decision(self, target_power: float, now: datetime) -> None:
        s = self._settings
        if not s.enable_phase_switching:
            return

        three_phase_on_w = 3 * s.min_amp * s.voltage
        one_phase_off_w = 1 * s.min_amp * s.voltage * 0.85  # hysteresis band below the 1-phase floor

        wants: int | None = None
        if target_power >= three_phase_on_w:
            wants = 3
        elif target_power < one_phase_off_w:
            wants = 1

        if wants is None or wants == self._phases:
            self._phase_crossing_since = None
            self._phase_crossing_wants = None
            return

        if self._phase_crossing_wants != wants:
            self._phase_crossing_wants = wants
            self._phase_crossing_since = now

        sustained = now - self._phase_crossing_since >= timedelta(seconds=s.phase_switch_sustain_s)
        cooled_down = self._last_phase_switch is None or (
            now - self._last_phase_switch >= timedelta(seconds=s.phase_switch_min_interval_s)
        )
        if sustained and cooled_down:
            self._phases = wants
            self._last_phase_switch = now
            self._phase_crossing_since = None
            self._phase_crossing_wants = None

    @staticmethod
    def _elapsed(since: datetime | None, now: datetime) -> float:
        return 0.0 if since is None else (now - since).total_seconds()

    def reset_session(self) -> None:
        """Clear per-session hysteresis state. Called by the control loop whenever no
        car is connected or a charge just finished, so that pausing decide() calls
        during that idle period doesn't leave a stale `_surplus_since`/`_deficit_since`
        timestamp around — without this, the next session would see an already-elapsed
        start_delay_s/stop_delay_s the moment a car is plugged back in, instead of
        actually waiting out the delay. `_phases`, `_last_amps`, and the phase-switch/
        amp-change cooldown timestamps are deliberately left untouched: they describe
        the physical wallbox/contactor state, not a specific charging session.
        """
        self._grid_history.clear()
        self._charging = False
        self._surplus_since = None
        self._deficit_since = None
        self._phase_crossing_since = None
        self._phase_crossing_wants = None


@dataclass
class LatestState:
    """Shared in-memory state: the control loop writes it every cycle, the web layer
    reads it to serve /api/status. Single asyncio process, no locking needed."""

    grid: GridReading | None = None
    wallbox: WallboxStatus | None = None
    decision: ControlDecision | None = None
    stale: bool = False
    updated_at: datetime | None = None


async def run_control_loop(
    settings: Settings,
    battery_client: BatterySystemClient,
    wallbox_client: WallboxClient,
    history: HistoryStore,
    state: LatestState,
) -> None:
    controller = SurplusController(settings)
    last_grid: GridReading | None = None
    last_applied: ControlDecision | None = None
    last_successful_read_at: datetime | None = None

    while True:
        now = datetime.now()

        try:
            wallbox = await wallbox_client.read_status()
        except Exception:
            logger.exception("Wallbox-Abfrage fehlgeschlagen, überspringe diesen Zyklus")
            await asyncio.sleep(settings.control_interval_s)
            continue

        if wallbox.car_state in _NO_SESSION_STATES:
            # No car connected, or the car finished charging: nothing to control, and no
            # point spending a Viessmann API call on it. Reset the controller's session
            # timers so the next real session starts start_delay_s/stop_delay_s clean
            # (see reset_session docstring), then just keep polling go-e — at the slower
            # goe_poll_interval_s — until a car shows up again.
            controller.reset_session()
            decision = ControlDecision(
                enable_charging=False,
                target_amps=last_applied.target_amps if last_applied else settings.min_amp,
                target_phases=last_applied.target_phases if last_applied else 1,
                reason="Kein Auto angeschlossen oder Ladevorgang abgeschlossen – Viessmann-Abfrage pausiert",
            )
            if not settings.dry_run:
                try:
                    await wallbox_client.set_charging(
                        enabled=False, amps=decision.target_amps, phases=decision.target_phases
                    )
                except Exception:
                    logger.exception("Konnte Ladeentscheidung nicht an die Wallbox übertragen")

            state.grid = last_grid
            state.wallbox = wallbox
            state.decision = decision
            state.stale = False
            state.updated_at = now
            last_applied = decision

            await asyncio.sleep(settings.goe_poll_interval_s)
            continue

        try:
            last_grid = await battery_client.read()
            last_successful_read_at = now
        except Exception:
            logger.exception("Battery-Systemabfrage fehlgeschlagen, verwende letzten bekannten Wert")
        grid = last_grid

        stale = False
        if grid is None:
            decision = ControlDecision(
                enable_charging=False, target_amps=settings.min_amp, target_phases=1,
                reason="Warte auf erste Messung vom Batteriesystem",
            )
        else:
            decision = controller.decide(grid, wallbox, now)
            # Real elapsed time since our own last successful read — not derived from
            # grid.timestamp, which for the mock client is an accelerated simulated
            # clock, not wall-clock time.
            stale_s = (now - last_successful_read_at).total_seconds()

            if stale_s > settings.viessmann_stale_stop_s:
                logger.warning("Batteriesystem-Daten seit %.0fs veraltet – stoppe Ladung", stale_s)
                stale = True
                decision = ControlDecision(
                    enable_charging=False, target_amps=decision.target_amps, target_phases=decision.target_phases,
                    reason=f"Daten seit {stale_s:.0f}s veraltet – Ladung gestoppt",
                )
            elif stale_s > settings.viessmann_stale_warn_s:
                logger.warning("Batteriesystem-Daten seit %.0fs veraltet – erhöhe Strom nicht weiter", stale_s)
                stale = True
                capped_amps = decision.target_amps if last_applied is None else min(
                    decision.target_amps, last_applied.target_amps
                )
                decision = ControlDecision(
                    enable_charging=decision.enable_charging, target_amps=capped_amps,
                    target_phases=decision.target_phases,
                    reason=f"Daten seit {stale_s:.0f}s veraltet – erhöhe Strom nicht weiter",
                )

        if not settings.dry_run:
            try:
                await wallbox_client.set_charging(
                    enabled=decision.enable_charging,
                    amps=decision.target_amps,
                    phases=decision.target_phases,
                )
            except Exception:
                logger.exception("Konnte Ladeentscheidung nicht an die Wallbox übertragen")

        if grid is not None:
            history.insert_sample(now, grid, wallbox, decision)

        state.grid = grid
        state.wallbox = wallbox
        state.decision = decision
        state.stale = stale
        state.updated_at = now
        last_applied = decision

        await asyncio.sleep(settings.control_interval_s)
