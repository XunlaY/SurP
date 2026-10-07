# AGENTS.md

Guidance for AI coding agents working in this repository. The `CLAUDE.md` file holds the
full architecture and domain conventions — **read it for details** (it is authoritative).
This file is a thin, durable extraction of the parts an agent needs up front so it already
knows the shape of the project before reading any source.

## Commands

```bash
pip install -e ".[dev]"     # install (src layout, editable)
pytest                       # run the full test suite
pytest tests/test_controller.py -q   # single file
surp run                     # start control loop + dashboard (PILOADING_MODE=mock by default)
surp auth-viessmann          # one-time interactive OAuth2/PKCE login (needs a browser)
```

## Scope for analysis (save tokens)

- Source lives only under `src/piloading/`; tests under `tests/`; everything else is
  config/docs (`pyproject.toml`, `README*`, `CLAUDE.md`, `deploy/`, `.env.example`).
- **Never search/read** `.venv/`, `.pytest_cache/`, `src/*.egg-info/`, `**/__pycache__/`,
  `.DS_Store`, or the local runtime DBs (`~/.local/share/piloading/history.db*`).
- **Never read `.env`** (live Viessmann credentials). Trust `.env.example` for shape.
- `src/piloading/controller.py` (450 lines) is the core — start there. The clients
  (`goe.py`, `viessmann.py`, `mock.py`, `base.py`, `viessmann_auth.py`) are small.
- The project is compact: ~1,700 lines source + ~1,200 lines tests. Don't assume size.

### Search/read scoping (do this, don't rely on tool config)

- There is **no linter, formatter, `setup.cfg`, `tox.ini`, or `Makefile`** — the repo has no
  tool config that restricts discovery. Scope all searches yourself.
- Anchor search/read paths under `src/piloading/` and `tests/` explicitly (e.g. glob
  `src/piloading/**/*.py`, never a bare `**/*.py` or repo-root search). A shortest-path
  glob hits the generated dirs otherwise.
- Never `cd` into or glob through `.venv/` — it contains a full copy of the installed
  stdlib/third-party packages and will flood a search with thousands of irrelevant files.
- Search sources with `glob("src/piloading/**/*.py")` / `glob("tests/**/*.py")`; read the
  file you need by absolute path instead of browsing.
- The single `pyproject.toml` is the only real config file — read it for deps/build/tooling;
  don't look for additional tool config that doesn't exist.

## Architecture (see CLAUDE.md for the full write-up)

- Single asyncio process: the control loop (`controller.py`) and the FastAPI server
  (`web/app.py`) run as sibling tasks via `asyncio.wait(..., return_when=FIRST_COMPLETED)` —
  if either dies, both are cancelled so systemd's `Restart=on-failure` recovers cleanly.
- Mock vs. live hardware is a config switch (`PILOADING_MODE`), not a code fork: controller
  and web app depend only on the `BatterySystemClient`/`WallboxClient` Protocols in
  `clients/base.py`. Only `cli.py` picks the concrete implementation.
- `SurplusController.decide()` is **pure** (no I/O, no `await`): takes readings + a
  `now: datetime`, returns a `ControlDecision`. All hysteresis/timing state lives in the
  controller instance, driven entirely by `now` — that is what makes it unit-testable.

## Domain gotchas (high-signal; details + validation in CLAUDE.md)

- **`COMPLETE` is ambiguous.** go-e reports `car=4` both for "car full, still connected" and
  when *we* disabled charging (deficit hysteresis or stale-data stop). The loop tracks a
  local `charging_stopped_by_us` flag and only treats `COMPLETE` as a real no-session when
  that flag is `False`. A phase switch can also cause a false `COMPLETE` blip, so it also
  suppresses the no-session transition for `PILOADING_PHASE_SWITCH_SETTLE_S` after our own
  last phase switch. In the genuine no-session case Viessmann polling pauses
  (`goe_poll_interval_s`, default 60s); otherwise it keeps the normal `control_interval_s`.
- **Wall-clock vs. simulated clock — never conflate.** `MockClock` (in `clients/mock.py`)
  is an accelerated, offset clock purely for the simulated solar/battery curves. All real
  control timing (`control_interval_s`, delays, staleness) must use `datetime.now()`, never
  a reading's `.timestamp`. Staleness uses the orchestrator's own `last_successful_read_at`.
- **go-e writes use `amp`/`frc`, not `amx`/`alw`.** `amx` doesn't exist on firmware 500 and
  `alw` is read-only (500); only the v2-native write keys work. `amp`/`frc` are flash-backed
  (~100k writes), so `set_charging` only writes a key when its value changed, same guard as
  `psm`. `frc`: Off=1 / On=2; Neutral=0 is never used.
- **go-e `psm` (phase switching) is unofficial/reverse-engineered.** Written only on an
  actual phase-count transition, not every cycle, to avoid spamming a key driving a physical
  contactor.
- **Viessmann feature names only partially confirmed.** Only `pcc.transfer.power.exchange`
  is corroborated externally; PV/battery names in `viessmann.py` are educated guesses. The
  client fetches the full feature list, searches by prefix, and logs the discovered list at
  startup — check that log line before assuming parsing is broken.
- **Night mode throttles polling during a sustained deficit.** `decide()` sets
  `decision.night_mode = True` once the deficit outlasts `night_mode_after_s` (600s); the
  loop then sleeps `night_mode_poll_interval_s` (600s) instead of `control_interval_s`, to
  avoid hitting Viessmann's daily API rate limit overnight. Clears instantly on surplus.

## Conventions (see CLAUDE.md)

- **Bilingual split**: dashboard + READMEs are English default + German. Strings live in
  `src/piloading/web/static/locales/{de,en}.json` — never inline in `index.html`. READMEs are
  `README.md` (EN) and `README.de.md` (DE), cross-linked. CLI `--help`, logs, commits, code,
  comments, and docstrings are English. Keep locales + READMEs in sync.
- **Never commit credentials**: `.env`, `*.db`, `.viessmann_auth.json`, `piloading_config/`
  are gitignored on purpose — `viessmann_auth.json` holds a live refresh token. Don't loosen
  `.gitignore`; double-check `git status` before committing anything touching auth/config.

## Testing

`pytest-asyncio` is in `auto` mode — async tests need no decorator. HTTP is mocked with
`respx`; the web app is tested in-process with `httpx.ASGITransport` (no real socket).
`tests/conftest.py` has shared builders for `GridReading`/`WallboxStatus`/`ControlDecision`.
