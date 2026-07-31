"""SQLite-backed history of control-loop samples, used by the web dashboard's
history chart. Stdlib sqlite3 only — no ORM needed for one table."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from piloading.models import ControlDecision, GridReading, WallboxStatus

_RETENTION_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts REAL PRIMARY KEY,
    grid_power_w REAL,
    pv_power_w REAL,
    battery_soc_pct REAL,
    wallbox_power_w REAL,
    wallbox_amps INTEGER,
    wallbox_phases INTEGER,
    car_state TEXT,
    charging INTEGER,
    reason TEXT
);
"""


class HistoryStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(_SCHEMA)
        self._conn.commit()
        self._prune()

    def _prune(self) -> None:
        cutoff = datetime.now().timestamp() - _RETENTION_DAYS * 86400
        self._conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        self._conn.commit()

    def insert_sample(
        self, now: datetime, grid: GridReading, wallbox: WallboxStatus, decision: ControlDecision
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO samples
                (ts, grid_power_w, pv_power_w, battery_soc_pct,
                 wallbox_power_w, wallbox_amps, wallbox_phases,
                 car_state, charging, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now.timestamp(),
                grid.grid_power_w,
                grid.pv_power_w,
                grid.battery_soc_pct,
                wallbox.power_w,
                wallbox.amps_effective,
                wallbox.phases,
                wallbox.car_state.name,
                int(decision.enable_charging),
                decision.reason,
            ),
        )
        self._conn.commit()

    def query_recent(self, hours: float = 24.0) -> list[dict[str, Any]]:
        cutoff = datetime.now().timestamp() - hours * 3600
        cursor = self._conn.execute("SELECT * FROM samples WHERE ts >= ? ORDER BY ts ASC", (cutoff,))
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def close(self) -> None:
        self._conn.close()
