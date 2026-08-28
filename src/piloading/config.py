"""Application settings, sourced from environment variables / a `.env` file.

All values are prefixed with PILOADING_ (e.g. PILOADING_MODE=live, PILOADING_GOE_HOST=...).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PILOADING_", env_file=".env", extra="ignore")

    # Runtime mode
    mode: Literal["mock", "live"] = "mock"
    dry_run: bool = False

    # go-e Wallbox (local API v2, no auth)
    goe_host: str = "192.168.1.50"
    # Poll interval used only while no car is connected or a charge just finished (see
    # run_control_loop) — the Viessmann API isn't queried during that time, so this is
    # purely how often go-e is asked whether a car has shown up again. While a session
    # is active, go-e is polled every control_interval_s instead, alongside Viessmann.
    goe_poll_interval_s: float = 60.0

    # Viessmann Vitocharge VX3 (cloud OAuth2/PKCE Developer API)
    viessmann_client_id: str | None = None
    viessmann_config_dir: Path = Field(default_factory=lambda: Path("~/.config/piloading").expanduser())
    viessmann_stale_warn_s: float = 300.0
    viessmann_stale_stop_s: float = 900.0
    invert_grid_power_sign: bool = False

    # Electrical setup
    voltage: float = 230.0
    min_amp: int = 6
    max_amp: int = 16
    enable_phase_switching: bool = True

    # Control algorithm timing/thresholds
    grid_power_smoothing_samples: int = 5
    target_grid_power_w: float = 0.0
    start_delay_s: float = 90.0
    stop_delay_s: float = 90.0
    phase_switch_sustain_s: float = 300.0
    phase_switch_min_interval_s: float = 600.0
    amp_change_cooldown_s: float = 15.0
    # How often the control loop re-evaluates the charging decision. Whenever a car is
    # connected and possibly charging, this is also how often *both* the go-e wallbox
    # and the Viessmann battery system are polled — one read of each per cycle. (When
    # no car is connected or a charge just finished, Viessmann isn't polled at all and
    # go-e falls back to goe_poll_interval_s instead — see run_control_loop.)
    control_interval_s: float = 10.0
    # How long a sustained deficit (insufficient surplus, e.g. after sunset) must persist
    # before the control loop switches to night mode, which polls at night_mode_poll_interval_s
    # instead of control_interval_s to avoid burning through Viessmann's daily API rate
    # limit overnight while there is no realistic chance of surplus returning soon.
    night_mode_after_s: float = 600.0
    # Poll interval used once night mode is active. Falls back to control_interval_s
    # immediately once surplus becomes sufficient again, which also resumes charging.
    night_mode_poll_interval_s: float = 600.0

    # Storage / web dashboard
    db_path: Path = Field(default_factory=lambda: Path("~/.local/share/piloading/history.db").expanduser())
    web_host: str = "0.0.0.0"
    web_port: int = 8080

    @property
    def viessmann_auth_file(self) -> Path:
        return self.viessmann_config_dir / "viessmann_auth.json"
