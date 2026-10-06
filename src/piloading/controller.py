"""Surplus-charging decision logic (pure, unit-testable) plus the async orchestration
loop that wires it to the real/mock clients, history storage, and shared web state.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from piloading.clients.base import BatterySystemClient, WallboxClient
from piloading.config import Settings
from piloading.history import HistoryStore
from piloading.models import CarState, ControlDecision, GridReading, WallboxStatus

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
        night_mode = False
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
            if self._charging:
                reason_key = "reason_charging_surplus"
                reason = (
                    f"Überschuss reicht ({target_power:.0f} W) – lade {self._phases}-phasig mit {self._last_amps} A"
                )
                reason_vars = {
                    "w": f"{target_power:.0f}",
                    "phases": str(self._phases),
                    "amps": str(self._last_amps),
                }
            else:
                reason_key = "reason_wait_start"
                reason = (
                    f"Überschuss seit {self._elapsed(self._surplus_since, now):.0f}s, "
                    f"warte auf Startschwelle ({s.start_delay_s:.0f}s)"
                )
                reason_vars = {
                    "elapsed": f"{self._elapsed(self._surplus_since, now):.0f}",
                    "delay": f"{s.start_delay_s:.0f}",
                }
        else:
            self._surplus_since = None
            if self._deficit_since is None:
                self._deficit_since = now
            if self._charging and now - self._deficit_since >= timedelta(seconds=s.stop_delay_s):
                self._charging = False
            deficit_elapsed = self._elapsed(self._deficit_since, now)
            night_mode = deficit_elapsed >= s.night_mode_after_s
            if not self._charging:
                reason_key = "reason_insufficient_surplus"
                reason = f"Kein ausreichender Überschuss seit {deficit_elapsed:.0f}s"
                reason_vars = {"elapsed": f"{deficit_elapsed:.0f}"}
                if night_mode:
                    reason += " – Nachtmodus aktiv, Abfrageintervall verlängert"
                    # The night-mode suffix is its own key so the dashboard can reuse it
                    # with the same translation in both languages.
                    reason_key = "reason_insufficient_surplus_night"
            else:
                reason_key = "reason_insufficient_stopping"
                reason = (
                    f"Überschuss reicht nicht mehr, Stoppschwelle in "
                    f"{max(0.0, s.stop_delay_s - deficit_elapsed):.0f}s (halte {self._last_amps} A)"
                )
                reason_vars = {
                    "remaining": f"{max(0.0, s.stop_delay_s - deficit_elapsed):.0f}",
                    "amps": str(self._last_amps),
                }

        return ControlDecision(
            enable_charging=self._charging,
            target_amps=self._last_amps,
            target_phases=self._phases,
            reason=reason,
            night_mode=night_mode,
            reason_key=reason_key,
            reason_vars=reason_vars,
        )

    def _update_phase_decision(self, target_power: float, now: datetime) -> None:
        s = self._settings
        if not s.enable_phase_switching:
            return

        three_phase_on_w = 3 * s.min_amp * s.voltage
        one_phase_off_w = 1 * s.min_amp * s.voltage * 0.85  # hysteresis band below the 1-phase floor

        if not self._charging:
            current_phase_sufficient_w = self._phases * s.min_amp * s.voltage
            if target_power < current_phase_sufficient_w:
                # Stopped, and surplus can no longer sustain the currently remembered
                # phase count. decide()'s own sufficient_w check is keyed off
                # self._phases, so if we left it pinned here, a session that stopped
                # while at 3-phase would keep requiring 3-phase-sized surplus to ever
                # resume — even once surplus has settled at a level that can only
                # support 1-phase charging. That surplus level typically falls
                # *between* the two thresholds below (enough to clear one_phase_off_w
                # but not three_phase_on_w), so it would never cross the dead zone
                # that would otherwise trigger a downshift, leaving charging stuck off
                # indefinitely despite real, adequate surplus. Since no current flows
                # through the contactor while stopped, resolving the phase count
                # immediately here is safe — no flapping risk for the sustain-time
                # hysteresis below to guard against — though we still rate-limit via
                # phase_switch_min_interval_s since the physical contactor is switched
                # either way. (Real incident: SurP stayed off for ~15 minutes
                # despite ~2-3kW of surplus, until the process happened to be
                # restarted, which reset self._phases to its default of 1.)
                wants = 3 if target_power >= three_phase_on_w else 1
                if wants != self._phases:
                    cooled_down = self._last_phase_switch is None or (
                        now - self._last_phase_switch >= timedelta(seconds=s.phase_switch_min_interval_s)
                    )
                    if cooled_down:
                        self._phases = wants
                        self._last_phase_switch = now
                self._phase_crossing_since = None
                self._phase_crossing_wants = None
                return
            # Otherwise the currently remembered phase count is still (or again)
            # sufficient, or charging simply hasn't started yet at the default
            # phase count — fall through to the normal hysteresis logic below.

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

    @property
    def last_phase_switch_at(self) -> datetime | None:
        """When the physical contactor was last toggled via a psm write, or None if
        never. Used by run_control_loop to recognize a go-e car_state=COMPLETE blip
        caused by that switch rather than the car actually finishing charging.
        """
        return self._last_phase_switch

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
    reads it to serve /api/status and signals Stop/Continue through it. Single asyncio
    process, no locking needed."""

    grid: GridReading | None = None
    wallbox: WallboxStatus | None = None
    decision: ControlDecision | None = None
    stale: bool = False
    updated_at: datetime | None = None
    # A user can manually pause SurP from the dashboard. While True, run_control_loop
    # touches neither the wallbox nor the battery (a manual charging current may be set
    # on the go-e that must not be overwritten) and instead blocks on `resume_condition`
    # until the flag is cleared again via Continue. The web layer flips it through
    # /api/stop and /api/continue, notifying on `resume_condition`; the control loop owns
    # the resume semantics.
    paused: bool = False
    resume_condition: asyncio.Condition = field(default_factory=asyncio.Condition)


async def run_control_loop(
    settings: Settings,
    battery_client: BatterySystemClient,
    wallbox_client: WallboxClient,
    history: HistoryStore,
    state: LatestState,
) -> None:
    controller = SurplusController(settings)
    last_grid: GridReading | None = None
    last_wallbox: WallboxStatus | None = None
    last_applied: ControlDecision | None = None
    last_successful_read_at: datetime | None = None
    # Tracks whether *we* were the ones who last told go-e to stop charging (hysteresis
    # deficit stop, a stale-data stop, or a user-initiated Stop below), as opposed to the
    # car stopping itself. go-e reports car_state=COMPLETE in all these cases —
    # indistinguishably — so without this we can't tell a real "car is full" from "we
    # just paused it, surplus may come back".
    charging_stopped_by_us = False

    while True:
        now = datetime.now()

        # User-initiated Stop from the dashboard. While paused we must not touch the
        # wallbox or battery at all — the user may have dialed in a manual charging
        # current on the go-e that SurP must not overwrite. We just keep serving the
        # last-known values plus an explicit "stopped" decision and block here until
        # `paused` is cleared again via Continue.
        if state.paused:
            stopped_decision = ControlDecision(
                enable_charging=False,
                target_amps=last_applied.target_amps if last_applied else settings.min_amp,
                target_phases=last_applied.target_phases if last_applied else 1,
                reason=(
                    "Vom Benutzer gestoppt – manuelle Steuerung an der Wallbox aktiv. "
                    "Zum Fortsetzen auf Weiter klicken."
                ),
                reason_key="reason_user_stopped",
                reason_vars=None,
            )
            state.grid = last_grid
            state.wallbox = last_wallbox
            state.decision = stopped_decision
            state.stale = False
            state.updated_at = now
            last_applied = stopped_decision
            # A manual stop now looks like a car-finished COMPLETE to go-e, so when we
            # resume we must NOT treat that COMPLETE as a permanent "car is full" — treat
            # it like a self-induced stop so SurP keeps trying to charge on Continue.
            charging_stopped_by_us = True
            # `asyncio.Event.wait()` returns immediately if already set, so an Event
            # can't express "block while true" — a Condition with an explicit predicate
            # is what actually blocks here until /api/continue flips `paused` to False.
            async with state.resume_condition:
                await state.resume_condition.wait_for(lambda: not state.paused)
            # Continue pressed: resume exactly like a restart of SurP. Reset the session
            # hysteresis (start_delay_s/stop_delay_s re-arm fresh) and keep the
            # charging_stopped_by_us=True above, so the very next go-e COMPLETE is not
            # mistaken for the car being full and SurP actively re-evaluates charging.
            controller.reset_session()
            logger.info("Ladesteuerung durch Benutzer fortgesetzt (Mainloop Fortsetzen)")
            continue

        try:
            wallbox = await wallbox_client.read_status()
        except Exception:
            logger.exception("Wallbox-Abfrage fehlgeschlagen, überspringe diesen Zyklus")
            await asyncio.sleep(settings.control_interval_s)
            continue
        last_wallbox = wallbox

        # A psm write toggles the physical contactor, which can plausibly make go-e
        # briefly report car_state=COMPLETE while it reconnects, even though the car is
        # still plugged in and charging fine — suspected root cause of a real-world case
        # where a 1->3-phase switch was immediately followed by SurP treating an
        # in-progress (not full) charge as finished. Treat any COMPLETE within
        # phase_switch_settle_s of our last switch the same as a self-induced stop: don't
        # reset the session or tell go-e to stop, just keep polling as if nothing happened.
        last_switch = controller.last_phase_switch_at
        recent_phase_switch = last_switch is not None and now - last_switch <= timedelta(
            seconds=settings.phase_switch_settle_s
        )
        if wallbox.car_state is CarState.COMPLETE and recent_phase_switch and not charging_stopped_by_us:
            logger.info(
                "COMPLETE kurz nach Phasenwechsel (%.0fs) – ignoriere, Ladevorgang läuft vermutlich weiter",
                (now - last_switch).total_seconds(),
            )

        # No car connected always means no session. COMPLETE only means no session when
        # the car stopped itself — if *we* were the last ones to disable charging (deficit
        # hysteresis or a stale-data stop), or a phase switch just flipped the contactor,
        # go-e still reports COMPLETE, but surplus may still return (or charging never
        # actually stopped), so we must keep polling Viessmann instead of pausing forever.
        no_session = wallbox.car_state is CarState.IDLE or (
            wallbox.car_state is CarState.COMPLETE and not charging_stopped_by_us and not recent_phase_switch
        )
        if no_session:
            # Nothing to control, and no point spending a Viessmann API call on it. Reset
            # the controller's session timers so the next real session starts
            # start_delay_s/stop_delay_s clean (see reset_session docstring), then just
            # keep polling go-e — at the slower goe_poll_interval_s — until a car shows up
            # again.
            controller.reset_session()
            charging_stopped_by_us = False
            if wallbox.car_state is CarState.IDLE:
                reason_key = "reason_no_car"
                reason = "Kein Auto angeschlossen – Viessmann-Abfrage pausiert"
                reason_vars = None
            else:
                reason_key = "reason_car_complete"
                reason = "Ladevorgang vom Auto beendet (Fahrzeug bleibt angeschlossen) – Viessmann-Abfrage pausiert"
                reason_vars = None
            decision = ControlDecision(
                enable_charging=False,
                target_amps=last_applied.target_amps if last_applied else settings.min_amp,
                target_phases=last_applied.target_phases if last_applied else 1,
                reason=reason,
                reason_key=reason_key,
                reason_vars=reason_vars,
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
                reason_key="reason_wait_first_reading",
                reason_vars=None,
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
                    reason_key="reason_stale_stop",
                    reason_vars={"elapsed": f"{stale_s:.0f}"},
                    night_mode=decision.night_mode,
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
                    reason_key="reason_stale_cap",
                    reason_vars={"elapsed": f"{stale_s:.0f}"},
                    night_mode=decision.night_mode,
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
        charging_stopped_by_us = not decision.enable_charging

        if grid is not None:
            history.insert_sample(now, grid, wallbox, decision)

        state.grid = grid
        state.wallbox = wallbox
        state.decision = decision
        state.stale = stale
        state.updated_at = now
        last_applied = decision

        sleep_s = settings.night_mode_poll_interval_s if decision.night_mode else settings.control_interval_s
        await asyncio.sleep(sleep_s)
