# CLAUDE.md

Guidance for Claude Code when working in this repository. See [README.md](README.md) for
user-facing setup/deployment instructions.

## Commands

```bash
pip install -e ".[dev]"     # install (src layout, editable)
pytest                       # run the full test suite
pytest tests/test_controller.py -q   # single file
piloading run                # start control loop + dashboard (PILOADING_MODE=mock by default)
piloading auth-viessmann     # one-time interactive OAuth2/PKCE login (needs a browser)
```

No linter/formatter is configured yet.

## Architecture

Single asyncio process (`cli.py`): the control loop (`controller.py`) and the FastAPI
server (`web/app.py`) run as sibling tasks via `asyncio.wait(..., return_when=FIRST_COMPLETED)`
— if either one dies, both are cancelled and the exception propagates so the whole process
exits (letting systemd's `Restart=on-failure` recover cleanly), rather than leaving a
half-alive process.

Mock vs. live hardware is a config switch (`PILOADING_MODE`), not a code fork: `controller.py`
and `web/app.py` only ever depend on the `BatterySystemClient`/`WallboxClient` Protocols in
`clients/base.py`. `cli.py` is the only place that picks a concrete implementation
(`clients/mock.py` vs. `clients/goe.py` + `clients/viessmann.py`).

`SurplusController.decide()` in `controller.py` is pure (no I/O, no `await`) — it takes
readings plus a `now: datetime` and returns a `ControlDecision`. All hysteresis/timing state
(surplus/deficit duration, phase-switch cooldown) lives inside the controller instance and is
driven entirely by the `now` passed in, which is what makes it unit-testable without real time
passing.

## Important conventions

- **Language split**: all user-facing text — CLI `--help`, log messages, dashboard labels,
  README — is German, matching how the user actually runs and reads this system. Code,
  comments, docstrings, and commit messages are English. Keep new user-visible strings German.
- **Never commit credentials**: `.env`, `*.db`, `.viessmann_auth.json`, `piloading_config/` are
  gitignored on purpose — `viessmann_auth.json` contains a live refresh token. Don't loosen
  `.gitignore` for these paths, and double-check `git status` before committing anything touching
  auth/config.
- **Wall-clock vs. simulated clock — do not conflate them.** `MockClock` (in `clients/mock.py`)
  runs an accelerated, offset clock purely to drive the simulated solar/battery curves; it has
  no relationship to real elapsed time. The control loop's own timing — `control_interval_s`,
  `start_delay_s`, `stop_delay_s`, `phase_switch_sustain_s`, and staleness detection — must
  always be computed from real `datetime.now()`, never from a reading's `.timestamp` field.
  Staleness in particular must be tracked via the orchestrator's own record of *when it last
  successfully read* (`last_successful_read_at` in `run_control_loop`), not derived from
  `grid.timestamp` — an earlier version of this code conflated the two, which made mock mode
  permanently report stale data. See `run_control_loop` in [controller.py](src/piloading/controller.py).
- **Viessmann feature names are only partially confirmed.** Only `pcc.transfer.power.exchange`
  (grid power) is corroborated by outside sources; PV/battery feature names in
  `clients/viessmann.py` are educated guesses. `ViessmannClient` fetches the full feature list
  and searches by prefix rather than hardcoding a narrow path, and logs the complete discovered
  feature list once at startup — if PV/battery data looks wrong or missing, check that log line
  before assuming the parsing logic is broken.
- **go-e `psm` (phase switching) is unofficial**, reverse-engineered by the community rather
  than documented by go-e. It's written only on an actual phase-count transition, not every
  cycle, to avoid spamming a key that drives a physical contactor.
- **Viessmann polling pauses when no car is connected — but `COMPLETE` is ambiguous.**
  `run_control_loop` reads go-e's `car_state` first, every cycle. `IDLE` always means no
  session. `COMPLETE` ("charge finished, car still connected") does *not* reliably mean
  the car is full: go-e reports the exact same `car=4` when *we* are the ones who last
  disabled charging (deficit hysteresis, or a stale-data stop) — confirmed against real
  hardware, sending `frc=Off` flips `car` from `WAIT_CAR`/`CHARGING` straight to
  `COMPLETE`. So the loop tracks a local `charging_stopped_by_us` flag (set from the final
  `decision.enable_charging` each normal cycle) and only treats `COMPLETE` as a real
  no-session state when that flag is `False` — i.e. the car stopped itself, not us. When
  `COMPLETE` follows our own stop, the loop keeps polling Viessmann at the normal
  `control_interval_s` instead of pausing, so surplus reappearing can still resume
  charging without needing an unplug/replug. Only in the genuine no-session case does the
  loop skip `battery_client.read()`, call `SurplusController.reset_session()`, and sleep
  `goe_poll_interval_s` (default 60s) instead of `control_interval_s` — go-e is still
  polled to notice a car showing up again, but Viessmann isn't hit while there's truly
  nothing to control. `reset_session()` clears the surplus/deficit/phase-crossing
  timestamps and the grid-power smoothing history, but deliberately leaves
  `_phases`/`_last_amps`/cooldown timestamps alone (physical wallbox/contactor state, not
  session state) — without this reset, a car plugged back in after a long idle stretch
  would see an already-elapsed `start_delay_s`/`stop_delay_s` from the stale timestamp and
  skip the delay instead of actually waiting it out.
- **go-e writes use `amp`/`frc`, not `amx`/`alw`.** Confirmed against real hardware: `amx`
  doesn't exist on this firmware (500) and `alw` is read-only (500) even with the "API v1"
  toggle enabled — the device only implements the v2-native write keys. `amp` sets current,
  `frc` enables/disables (Off=1/On=2; Neutral=0 is never used, since that hands control back to
  the charger's own logic). Unlike the old `amx` assumption (RAM-only), `amp`/`frc` are
  flash-backed and rated for ~100k writes, so `set_charging` in
  [goe.py](src/piloading/clients/goe.py) only sends a key when its value actually changed since
  the last call — the same guard already used for `psm`.

## Testing

`pytest-asyncio` is in `auto` mode (see `pyproject.toml`) — async test functions don't need a
decorator. HTTP calls are mocked with `respx` (Viessmann, go-e clients); the web app is tested
in-process with `httpx.ASGITransport` (no real socket). `tests/conftest.py` has shared builders
for `GridReading`/`WallboxStatus`/`ControlDecision`.
