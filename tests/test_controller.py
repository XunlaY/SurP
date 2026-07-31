"""Unit tests for SurplusController.decide() — pure, no I/O, no asyncio needed.

Each test drives decide() through a synthetic sequence of (grid, wallbox, now) calls
and asserts on the returned ControlDecision.
"""

from __future__ import annotations

from datetime import timedelta

from conftest import grid, make_settings, wallbox

from piloading.controller import SurplusController


def test_does_not_start_until_start_delay_elapsed(base_time):
    settings = make_settings(start_delay_s=90.0)
    controller = SurplusController(settings)

    d1 = controller.decide(grid(-2000, base_time), wallbox(base_time, charging=False), base_time)
    assert d1.enable_charging is False

    t2 = base_time + timedelta(seconds=60)
    d2 = controller.decide(grid(-2000, t2), wallbox(t2, charging=False), t2)
    assert d2.enable_charging is False

    t3 = base_time + timedelta(seconds=91)
    d3 = controller.decide(grid(-2000, t3), wallbox(t3, charging=False), t3)
    assert d3.enable_charging is True
    assert d3.target_amps == 9  # round(2000 / 230)


def test_does_not_stop_until_stop_delay_elapsed_and_holds_amps(base_time):
    settings = make_settings(start_delay_s=90.0, stop_delay_s=90.0)
    controller = SurplusController(settings)

    t0 = base_time
    controller.decide(grid(-2000, t0), wallbox(t0, charging=False), t0)
    t1 = base_time + timedelta(seconds=90)
    started = controller.decide(grid(-2000, t1), wallbox(t1, charging=False), t1)
    assert started.enable_charging is True
    assert started.target_amps == 9

    # Wallbox now reflects the applied command: charging at 9A/1phase = 2070W.
    t2 = t1 + timedelta(seconds=10)
    deficit_start = controller.decide(grid(800, t2), wallbox(t2, charging=True, power_w=2070), t2)
    assert deficit_start.enable_charging is True  # inside grace window
    assert deficit_start.target_amps == 9  # held, not recomputed

    t3 = t2 + timedelta(seconds=30)
    still_grace = controller.decide(grid(800, t3), wallbox(t3, charging=True, power_w=2070), t3)
    assert still_grace.enable_charging is True
    assert still_grace.target_amps == 9

    t4 = t2 + timedelta(seconds=91)
    stopped = controller.decide(grid(800, t4), wallbox(t4, charging=True, power_w=2070), t4)
    assert stopped.enable_charging is False


def test_does_not_flap_on_brief_interruptions(base_time):
    settings = make_settings(start_delay_s=90.0)
    controller = SurplusController(settings)

    t0 = base_time
    d0 = controller.decide(grid(-2000, t0), wallbox(t0, charging=False), t0)
    assert d0.enable_charging is False

    t1 = base_time + timedelta(seconds=50)
    d1 = controller.decide(grid(-2000, t1), wallbox(t1, charging=False), t1)
    assert d1.enable_charging is False

    # Brief deficit interrupts the surplus streak — this must reset the start timer.
    t2 = base_time + timedelta(seconds=60)
    d2 = controller.decide(grid(2000, t2), wallbox(t2, charging=False), t2)
    assert d2.enable_charging is False

    t3 = base_time + timedelta(seconds=65)
    controller.decide(grid(-2000, t3), wallbox(t3, charging=False), t3)

    # Only 80s of *consecutive* surplus since t3 — must still not have started.
    t4 = t3 + timedelta(seconds=80)
    d4 = controller.decide(grid(-2000, t4), wallbox(t4, charging=False), t4)
    assert d4.enable_charging is False

    # 95s consecutive surplus since t3 — now it should start.
    t5 = t3 + timedelta(seconds=95)
    d5 = controller.decide(grid(-2000, t5), wallbox(t5, charging=False), t5)
    assert d5.enable_charging is True


def test_phase_switch_requires_sustain_and_respects_min_interval(base_time):
    settings = make_settings(
        phase_switch_sustain_s=300.0,
        phase_switch_min_interval_s=600.0,
        min_amp=6,
        voltage=230.0,
    )
    controller = SurplusController(settings)
    # Keep the wallbox permanently "off" from the client's point of view so
    # target_power tracks the grid reading directly, isolating phase logic.
    wb = wallbox(base_time, charging=False)

    t0 = base_time
    d0 = controller.decide(grid(-6000, t0), wb, t0)  # big surplus, wants 3-phase
    assert d0.target_phases == 1

    t1 = base_time + timedelta(seconds=299)
    d1 = controller.decide(grid(-6000, t1), wb, t1)
    assert d1.target_phases == 1  # not sustained long enough yet

    t2 = base_time + timedelta(seconds=300)
    d2 = controller.decide(grid(-6000, t2), wb, t2)
    assert d2.target_phases == 3  # sustained for phase_switch_sustain_s

    # Now push it back toward 1-phase; sustain elapses well before min_interval does.
    t3 = t2 + timedelta(seconds=10)
    controller.decide(grid(2000, t3), wb, t3)  # deficit -> wants 1-phase

    t4 = t2 + timedelta(seconds=320)  # 310s since the crossing at t3: sustain met, but min_interval isn't
    d4 = controller.decide(grid(2000, t4), wb, t4)
    assert d4.target_phases == 3  # blocked by phase_switch_min_interval_s

    t5 = t2 + timedelta(seconds=600)  # min_interval satisfied, sustain long since satisfied
    d5 = controller.decide(grid(2000, t5), wb, t5)
    assert d5.target_phases == 1


def test_amps_are_clamped_to_min_and_max(base_time):
    settings = make_settings(
        start_delay_s=0.0, min_amp=6, max_amp=16, voltage=230.0, enable_phase_switching=False
    )

    huge_surplus = SurplusController(settings)
    d_huge = huge_surplus.decide(grid(-100_000, base_time), wallbox(base_time, charging=False), base_time)
    assert d_huge.enable_charging is True
    assert d_huge.target_amps == 16

    barely_enough = SurplusController(settings)
    d_low = barely_enough.decide(grid(-1400, base_time), wallbox(base_time, charging=False), base_time)
    assert d_low.enable_charging is True
    assert d_low.target_amps == 6
