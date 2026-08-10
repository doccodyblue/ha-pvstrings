"""SQLite persistence.

All timestamps are Unix epoch seconds in UTC.  Local time is derived on
display and never stored: DST creates duplicate and missing local hours, which
makes local time unusable as a primary key.

Every method here is synchronous and blocking.  The Home Assistant layer is
responsible for pushing calls into the executor -- the core must stay usable
from a plain script.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .config import GeometrySegment
from .quality import VALUE_MEASURED

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS string_geometry (
    string_id         TEXT    NOT NULL,
    valid_from_ts_utc INTEGER NOT NULL,
    azimuth_deg       REAL    NOT NULL,
    tilt_deg          REAL    NOT NULL,
    kwp               REAL    NOT NULL,
    temp_coeff        REAL    NOT NULL DEFAULT -0.004,
    note              TEXT,
    PRIMARY KEY (string_id, valid_from_ts_utc)
);

CREATE TABLE IF NOT EXISTS string_5min (
    ts_utc            INTEGER NOT NULL,
    string_id         TEXT    NOT NULL,
    energy_wh         REAL,
    power_mean_w      REAL,
    coverage          REAL    NOT NULL,
    sample_count      INTEGER NOT NULL,
    limit_commanded_w REAL,
    limit_binding     INTEGER,
    value_kind        TEXT    NOT NULL,
    PRIMARY KEY (ts_utc, string_id)
);
CREATE INDEX IF NOT EXISTS ix_string_5min_string ON string_5min (string_id, ts_utc);

CREATE TABLE IF NOT EXISTS weather_actual_5min (
    ts_utc       INTEGER PRIMARY KEY,
    temp_c       REAL,
    humidity_pct REAL,
    wind_ms      REAL,
    rain_mm      REAL,
    pressure_hpa REAL,
    ghi_wm2      REAL,
    lux          REAL
);

CREATE TABLE IF NOT EXISTS weather_forecast (
    issued_at_utc        INTEGER NOT NULL,
    ts_utc               INTEGER NOT NULL,
    source               TEXT    NOT NULL,
    horizon_h            INTEGER NOT NULL,
    ghi_wm2              REAL,
    dni_wm2              REAL,
    dhi_wm2              REAL,
    temp_c               REAL,
    clouds_pct           REAL,
    wind_ms              REAL,
    humidity_pct         REAL,
    rain_mm              REAL,
    pressure_hpa         REAL,
    components_plausible INTEGER,
    PRIMARY KEY (issued_at_utc, ts_utc, source)
);
CREATE INDEX IF NOT EXISTS ix_weather_forecast_ts ON weather_forecast (ts_utc, source);

CREATE TABLE IF NOT EXISTS plant_state_5min (
    ts_utc          INTEGER PRIMARY KEY,
    battery_soc_pct REAL,
    battery_power_w REAL,
    grid_power_w    REAL,
    house_load_w    REAL
);

CREATE TABLE IF NOT EXISTS string_hourly (
    ts_utc             INTEGER NOT NULL,
    string_id          TEXT    NOT NULL,
    energy_kwh         REAL,
    coverage           REAL    NOT NULL,
    curtailed_fraction REAL    NOT NULL,
    limit_min_w        REAL,
    limit_max_w        REAL,
    limit_mean_w       REAL,
    value_kind         TEXT    NOT NULL,
    quality            TEXT    NOT NULL,
    PRIMARY KEY (ts_utc, string_id)
);
CREATE INDEX IF NOT EXISTS ix_string_hourly_string ON string_hourly (string_id, ts_utc);

CREATE TABLE IF NOT EXISTS forecast_log (
    issued_at_utc INTEGER NOT NULL,
    ts_utc        INTEGER NOT NULL,
    string_id     TEXT    NOT NULL,
    potential_kwh REAL    NOT NULL,
    method        TEXT    NOT NULL,
    PRIMARY KEY (issued_at_utc, ts_utc, string_id)
);
CREATE INDEX IF NOT EXISTS ix_forecast_log_ts ON forecast_log (ts_utc, string_id);

CREATE TABLE IF NOT EXISTS shading_obs (
    ts_utc        INTEGER NOT NULL,
    string_id     TEXT    NOT NULL,
    azimuth_deg   REAL    NOT NULL,
    elevation_deg REAL    NOT NULL,
    ratio         REAL    NOT NULL,
    weight        REAL    NOT NULL,
    PRIMARY KEY (ts_utc, string_id)
);

CREATE TABLE IF NOT EXISTS model_effects (
    scope      TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      REAL NOT NULL DEFAULT 0.0,
    n_eff      REAL NOT NULL DEFAULT 0.0,
    updated_at INTEGER,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS ghi_bias (
    source      TEXT    NOT NULL,
    hour_local  INTEGER NOT NULL,
    horizon_bkt TEXT    NOT NULL,
    log_factor  REAL    NOT NULL DEFAULT 0.0,
    n_eff       REAL    NOT NULL DEFAULT 0.0,
    updated_at  INTEGER,
    PRIMARY KEY (source, hour_local, horizon_bkt)
);

CREATE TABLE IF NOT EXISTS exclusions (
    ts_utc    INTEGER NOT NULL,
    string_id TEXT    NOT NULL DEFAULT '',
    reason    TEXT    NOT NULL,
    detail    TEXT,
    PRIMARY KEY (ts_utc, string_id, reason)
);

CREATE TABLE IF NOT EXISTS learning_cursor (
    name       TEXT PRIMARY KEY,
    ts_utc     INTEGER NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class HourlyRow:
    ts_utc: int
    string_id: str
    energy_kwh: float | None
    coverage: float
    curtailed_fraction: float
    limit_min_w: float | None
    limit_max_w: float | None
    limit_mean_w: float | None
    value_kind: str
    quality: str


class Store:
    """Thread-safe SQLite wrapper.

    A single connection guarded by a lock is enough here: writes are small and
    infrequent (one batch every five minutes), and it keeps the WAL file from
    being touched by several connections at once on network storage.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._geometry_cache: dict[str, list[GeometrySegment]] = {}

    # -- lifecycle --------------------------------------------------------- #

    def connect(self) -> None:
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=30.0
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        self._conn = conn
        self._migrate()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Store":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _migrate(self) -> None:
        assert self._conn is not None
        with self._lock:
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            self._conn.executescript(_SCHEMA)
            if current < SCHEMA_VERSION:
                _LOGGER.debug(
                    "pvstrings schema %s -> %s at %s", current, SCHEMA_VERSION, self.path
                )
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # -- geometry ---------------------------------------------------------- #

    def add_geometry(self, string_id: str, segment: GeometrySegment) -> None:
        """Append (or replace at the same timestamp) a validity period."""
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO string_geometry
                    (string_id, valid_from_ts_utc, azimuth_deg, tilt_deg, kwp,
                     temp_coeff, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (string_id, valid_from_ts_utc) DO UPDATE SET
                    azimuth_deg = excluded.azimuth_deg,
                    tilt_deg    = excluded.tilt_deg,
                    kwp         = excluded.kwp,
                    temp_coeff  = excluded.temp_coeff,
                    note        = excluded.note
                """,
                segment.as_row(string_id),
            )
        self._geometry_cache.pop(string_id, None)

    def replace_latest_geometry(self, string_id: str, segment: GeometrySegment) -> None:
        """Correct a typo: overwrite the newest segment instead of appending one."""
        history = self.geometry_history(string_id)
        with self._tx() as conn:
            if history:
                conn.execute(
                    "DELETE FROM string_geometry "
                    "WHERE string_id = ? AND valid_from_ts_utc = ?",
                    (string_id, history[-1].valid_from_ts_utc),
                )
            conn.execute(
                """
                INSERT INTO string_geometry
                    (string_id, valid_from_ts_utc, azimuth_deg, tilt_deg, kwp,
                     temp_coeff, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                segment.as_row(string_id),
            )
        self._geometry_cache.pop(string_id, None)

    def delete_geometry(self, string_id: str, valid_from_ts_utc: int) -> None:
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM string_geometry "
                "WHERE string_id = ? AND valid_from_ts_utc = ?",
                (string_id, valid_from_ts_utc),
            )
        self._geometry_cache.pop(string_id, None)

    def geometry_history(self, string_id: str) -> list[GeometrySegment]:
        cached = self._geometry_cache.get(string_id)
        if cached is not None:
            return cached
        rows = self._query(
            "SELECT valid_from_ts_utc, azimuth_deg, tilt_deg, kwp, temp_coeff, note "
            "FROM string_geometry WHERE string_id = ? ORDER BY valid_from_ts_utc",
            (string_id,),
        )
        history = [
            GeometrySegment(
                valid_from_ts_utc=row["valid_from_ts_utc"],
                azimuth_deg=row["azimuth_deg"],
                tilt_deg=row["tilt_deg"],
                kwp=row["kwp"],
                temp_coeff=row["temp_coeff"],
                note=row["note"],
            )
            for row in rows
        ]
        self._geometry_cache[string_id] = history
        return history

    def geometry_at(self, string_id: str, ts_utc: int) -> GeometrySegment | None:
        """The segment in force at ``ts_utc``.

        Never take "the current configuration" for a past timestamp -- that is
        exactly the silent, season-travelling error this project exists to
        remove.
        """
        best: GeometrySegment | None = None
        for segment in self.geometry_history(string_id):
            if segment.valid_from_ts_utc <= ts_utc:
                best = segment
            else:
                break
        if best is None:
            # Before the first recorded segment we fall back to the earliest
            # one rather than refusing: better a slightly wrong old geometry
            # than a hole in the history.
            history = self.geometry_history(string_id)
            best = history[0] if history else None
        return best

    def known_string_ids(self) -> list[str]:
        return [
            row["string_id"]
            for row in self._query(
                "SELECT DISTINCT string_id FROM string_geometry ORDER BY string_id"
            )
        ]

    # -- five-minute data -------------------------------------------------- #

    def upsert_5min(self, rows: Iterable[tuple[Any, ...]]) -> int:
        """``(ts_utc, string_id, energy_wh, power_mean_w, coverage, sample_count,
        limit_commanded_w, limit_binding, value_kind)``"""
        payload = list(rows)
        if not payload:
            return 0
        with self._tx() as conn:
            conn.executemany(
                """
                INSERT INTO string_5min
                    (ts_utc, string_id, energy_wh, power_mean_w, coverage,
                     sample_count, limit_commanded_w, limit_binding, value_kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (ts_utc, string_id) DO UPDATE SET
                    energy_wh         = excluded.energy_wh,
                    power_mean_w      = excluded.power_mean_w,
                    coverage          = excluded.coverage,
                    sample_count      = excluded.sample_count,
                    limit_commanded_w = excluded.limit_commanded_w,
                    limit_binding     = excluded.limit_binding,
                    value_kind        = excluded.value_kind
                """,
                payload,
            )
        return len(payload)

    def fivemin_range(
        self, string_id: str, start_ts: int, end_ts: int
    ) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM string_5min "
            "WHERE string_id = ? AND ts_utc >= ? AND ts_utc < ? ORDER BY ts_utc",
            (string_id, start_ts, end_ts),
        )

    def energy_kwh_between(self, start_ts: int, end_ts: int, string_id: str | None = None) -> float:
        sql = (
            "SELECT COALESCE(SUM(energy_wh), 0) AS wh FROM string_5min "
            "WHERE ts_utc >= ? AND ts_utc < ?"
        )
        params: list[Any] = [start_ts, end_ts]
        if string_id:
            sql += " AND string_id = ?"
            params.append(string_id)
        return float(self._query(sql, params)[0]["wh"]) / 1000.0

    def update_curtailment_flags(
        self, rows: Iterable[tuple[int | None, int, str]]
    ) -> None:
        """``(limit_binding, ts_utc, string_id)`` -- set once physics is known.

        The verdict is re-evaluated whenever physics is recomputed, so it has
        to be reversible: a row previously marked ``lower_bound`` must return
        to ``measured`` when the limit turns out not to have been binding after
        all.  Otherwise a single bad physics estimate censors that interval
        permanently.  ``reconstructed`` is left alone -- that kind was not
        derived from the binding test.
        """
        payload = list(rows)
        if not payload:
            return
        with self._tx() as conn:
            conn.executemany(
                "UPDATE string_5min SET limit_binding = ?, value_kind = CASE "
                "  WHEN ? = 1 THEN 'lower_bound' "
                "  WHEN ? = 0 AND value_kind = 'lower_bound' THEN 'measured' "
                "  ELSE value_kind END "
                "WHERE ts_utc = ? AND string_id = ?",
                [(b, b, b, ts, sid) for b, ts, sid in payload],
            )

    def set_value_kind(self, ts_utc: int, string_id: str, value_kind: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE string_5min SET value_kind = ? WHERE ts_utc = ? AND string_id = ?",
                (value_kind, ts_utc, string_id),
            )

    def interval_stats(self, string_id: str, start_ts: int, end_ts: int) -> dict[str, Any]:
        """Quality breakdown of the five-minute rows in a window.

        This is what tells "the forecast is wrong" apart from "the data going
        in is wrong", so it belongs in front of the user rather than only in a
        diagnostics download.
        """
        rows = self._query(
            "SELECT coverage, value_kind, limit_binding, sample_count "
            "FROM string_5min WHERE string_id = ? AND ts_utc >= ? AND ts_utc < ?",
            (string_id, start_ts, end_ts),
        )
        if not rows:
            return {
                "intervals": 0,
                "coverage_mean": None,
                "samples_mean": None,
                "curtailed_fraction": None,
                "value_kinds": {},
            }
        kinds: dict[str, int] = {}
        for row in rows:
            kinds[row["value_kind"]] = kinds.get(row["value_kind"], 0) + 1
        known = [r["limit_binding"] for r in rows if r["limit_binding"] is not None]
        return {
            "intervals": len(rows),
            "coverage_mean": round(
                sum(r["coverage"] for r in rows) / len(rows), 3
            ),
            "samples_mean": round(
                sum(r["sample_count"] for r in rows) / len(rows), 1
            ),
            "curtailed_fraction": (
                round(sum(1 for b in known if b) / len(known), 3) if known else None
            ),
            "value_kinds": kinds,
        }

    # -- hourly ------------------------------------------------------------ #

    def upsert_hourly(self, rows: Iterable[tuple[Any, ...]]) -> int:
        payload = list(rows)
        if not payload:
            return 0
        with self._tx() as conn:
            conn.executemany(
                """
                INSERT INTO string_hourly
                    (ts_utc, string_id, energy_kwh, coverage, curtailed_fraction,
                     limit_min_w, limit_max_w, limit_mean_w, value_kind, quality)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (ts_utc, string_id) DO UPDATE SET
                    energy_kwh         = excluded.energy_kwh,
                    coverage           = excluded.coverage,
                    curtailed_fraction = excluded.curtailed_fraction,
                    limit_min_w        = excluded.limit_min_w,
                    limit_max_w        = excluded.limit_max_w,
                    limit_mean_w       = excluded.limit_mean_w,
                    value_kind         = excluded.value_kind,
                    quality            = excluded.quality
                """,
                payload,
            )
        return len(payload)

    def hourly_range(
        self, start_ts: int, end_ts: int, string_id: str | None = None
    ) -> list[HourlyRow]:
        sql = "SELECT * FROM string_hourly WHERE ts_utc >= ? AND ts_utc < ?"
        params: list[Any] = [start_ts, end_ts]
        if string_id:
            sql += " AND string_id = ?"
            params.append(string_id)
        sql += " ORDER BY ts_utc"
        return [
            HourlyRow(
                ts_utc=row["ts_utc"],
                string_id=row["string_id"],
                energy_kwh=row["energy_kwh"],
                coverage=row["coverage"],
                curtailed_fraction=row["curtailed_fraction"],
                limit_min_w=row["limit_min_w"],
                limit_max_w=row["limit_max_w"],
                limit_mean_w=row["limit_mean_w"],
                value_kind=row["value_kind"],
                quality=row["quality"],
            )
            for row in self._query(sql, params)
        ]

    # -- weather ----------------------------------------------------------- #

    def upsert_weather_actual(self, rows: Iterable[tuple[Any, ...]]) -> None:
        payload = list(rows)
        if not payload:
            return
        with self._tx() as conn:
            conn.executemany(
                """
                INSERT INTO weather_actual_5min
                    (ts_utc, temp_c, humidity_pct, wind_ms, rain_mm, pressure_hpa,
                     ghi_wm2, lux)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (ts_utc) DO UPDATE SET
                    temp_c       = COALESCE(excluded.temp_c, temp_c),
                    humidity_pct = COALESCE(excluded.humidity_pct, humidity_pct),
                    wind_ms      = COALESCE(excluded.wind_ms, wind_ms),
                    rain_mm      = COALESCE(excluded.rain_mm, rain_mm),
                    pressure_hpa = COALESCE(excluded.pressure_hpa, pressure_hpa),
                    ghi_wm2      = COALESCE(excluded.ghi_wm2, ghi_wm2),
                    lux          = COALESCE(excluded.lux, lux)
                """,
                payload,
            )

    def weather_actual_range(self, start_ts: int, end_ts: int) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM weather_actual_5min WHERE ts_utc >= ? AND ts_utc < ? "
            "ORDER BY ts_utc",
            (start_ts, end_ts),
        )

    def upsert_weather_forecast(self, rows: Iterable[tuple[Any, ...]]) -> int:
        payload = list(rows)
        if not payload:
            return 0
        with self._tx() as conn:
            conn.executemany(
                """
                INSERT INTO weather_forecast
                    (issued_at_utc, ts_utc, source, horizon_h, ghi_wm2, dni_wm2,
                     dhi_wm2, temp_c, clouds_pct, wind_ms, humidity_pct, rain_mm,
                     pressure_hpa, components_plausible)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (issued_at_utc, ts_utc, source) DO UPDATE SET
                    ghi_wm2              = excluded.ghi_wm2,
                    dni_wm2              = excluded.dni_wm2,
                    dhi_wm2              = excluded.dhi_wm2,
                    temp_c               = excluded.temp_c,
                    clouds_pct           = excluded.clouds_pct,
                    wind_ms              = excluded.wind_ms,
                    humidity_pct         = excluded.humidity_pct,
                    rain_mm              = excluded.rain_mm,
                    pressure_hpa         = excluded.pressure_hpa,
                    components_plausible = excluded.components_plausible
                """,
                payload,
            )
        return len(payload)

    def latest_forecast(
        self, start_ts: int, end_ts: int, source: str
    ) -> list[sqlite3.Row]:
        """Most recently issued forecast row for every target timestamp."""
        return self._query(
            """
            SELECT f.* FROM weather_forecast f
            JOIN (
                SELECT ts_utc, MAX(issued_at_utc) AS issued
                FROM weather_forecast
                WHERE source = ? AND ts_utc >= ? AND ts_utc < ?
                GROUP BY ts_utc
            ) latest
              ON latest.ts_utc = f.ts_utc AND latest.issued = f.issued_at_utc
            WHERE f.source = ?
            ORDER BY f.ts_utc
            """,
            (source, start_ts, end_ts, source),
        )

    def forecast_for_verification(
        self, start_ts: int, end_ts: int, source: str, max_horizon_h: int = 48
    ) -> list[sqlite3.Row]:
        """All forecast issues for a past window, for bias learning."""
        return self._query(
            "SELECT * FROM weather_forecast WHERE source = ? AND ts_utc >= ? "
            "AND ts_utc < ? AND horizon_h <= ? ORDER BY ts_utc, horizon_h",
            (source, start_ts, end_ts, max_horizon_h),
        )

    # -- plant state ------------------------------------------------------- #

    def upsert_plant_state(self, rows: Iterable[tuple[Any, ...]]) -> None:
        payload = list(rows)
        if not payload:
            return
        with self._tx() as conn:
            conn.executemany(
                """
                INSERT INTO plant_state_5min
                    (ts_utc, battery_soc_pct, battery_power_w, grid_power_w,
                     house_load_w)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (ts_utc) DO UPDATE SET
                    battery_soc_pct = COALESCE(excluded.battery_soc_pct, battery_soc_pct),
                    battery_power_w = COALESCE(excluded.battery_power_w, battery_power_w),
                    grid_power_w    = COALESCE(excluded.grid_power_w, grid_power_w),
                    house_load_w    = COALESCE(excluded.house_load_w, house_load_w)
                """,
                payload,
            )

    def grid_energy_kwh(self, start_ts: int, end_ts: int) -> tuple[float, float]:
        """Imported and exported kWh in the window, from 5-minute means."""
        rows = self._query(
            "SELECT grid_power_w FROM plant_state_5min "
            "WHERE ts_utc >= ? AND ts_utc < ? AND grid_power_w IS NOT NULL",
            (start_ts, end_ts),
        )
        hours = 300.0 / 3600.0
        imported = sum(r["grid_power_w"] for r in rows if r["grid_power_w"] > 0)
        exported = -sum(r["grid_power_w"] for r in rows if r["grid_power_w"] < 0)
        return imported * hours / 1000.0, exported * hours / 1000.0

    # -- forecast log ------------------------------------------------------ #

    def log_forecast(self, rows: Iterable[tuple[Any, ...]]) -> int:
        payload = list(rows)
        if not payload:
            return 0
        with self._tx() as conn:
            conn.executemany(
                """
                INSERT INTO forecast_log
                    (issued_at_utc, ts_utc, string_id, potential_kwh, method)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (issued_at_utc, ts_utc, string_id) DO UPDATE SET
                    potential_kwh = excluded.potential_kwh,
                    method        = excluded.method
                """,
                payload,
            )
        return len(payload)

    def forecast_vs_actual(
        self, start_ts: int, end_ts: int, lead_time_h: float = 0.0
    ) -> list[sqlite3.Row]:
        """Pair measured hours with the forecast that was available beforehand.

        ``lead_time_h`` is how far ahead the forecast had to be issued.  Zero
        means "the last forecast before the hour started"; 24 scores day-ahead
        quality.  Scoring against a forecast issued *during* the hour would be
        hindsight and is not possible through this method.
        """
        lead_seconds = int(lead_time_h * 3600)
        return self._query(
            """
            SELECT h.ts_utc, h.string_id, h.energy_kwh, h.quality, h.value_kind,
                   h.curtailed_fraction, h.coverage,
                   (SELECT f.potential_kwh FROM forecast_log f
                     WHERE f.ts_utc = h.ts_utc
                       AND f.string_id = h.string_id
                       AND f.issued_at_utc <= h.ts_utc - ?
                     ORDER BY f.issued_at_utc DESC LIMIT 1) AS potential_kwh
            FROM string_hourly h
            WHERE h.ts_utc >= ? AND h.ts_utc < ?
            ORDER BY h.ts_utc
            """,
            (lead_seconds, start_ts, end_ts),
        )

    # -- shading ----------------------------------------------------------- #

    def add_shading_obs(self, rows: Iterable[tuple[Any, ...]]) -> None:
        payload = list(rows)
        if not payload:
            return
        with self._tx() as conn:
            conn.executemany(
                """
                INSERT INTO shading_obs
                    (ts_utc, string_id, azimuth_deg, elevation_deg, ratio, weight)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (ts_utc, string_id) DO UPDATE SET
                    azimuth_deg   = excluded.azimuth_deg,
                    elevation_deg = excluded.elevation_deg,
                    ratio         = excluded.ratio,
                    weight        = excluded.weight
                """,
                payload,
            )

    def shading_count(self, string_id: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM shading_obs"
        params: list[Any] = []
        if string_id:
            sql += " WHERE string_id = ?"
            params.append(string_id)
        return int(self._query(sql, params)[0]["n"])

    # -- model state ------------------------------------------------------- #

    def load_effects(self, scope: str) -> dict[str, tuple[float, float]]:
        return {
            row["key"]: (row["value"], row["n_eff"])
            for row in self._query(
                "SELECT key, value, n_eff FROM model_effects WHERE scope = ?", (scope,)
            )
        }

    def save_effects(
        self, scope: str, effects: dict[str, tuple[float, float]], updated_at: int
    ) -> None:
        if not effects:
            return
        with self._tx() as conn:
            conn.executemany(
                """
                INSERT INTO model_effects (scope, key, value, n_eff, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (scope, key) DO UPDATE SET
                    value      = excluded.value,
                    n_eff      = excluded.n_eff,
                    updated_at = excluded.updated_at
                """,
                [
                    (scope, key, value, n_eff, updated_at)
                    for key, (value, n_eff) in effects.items()
                ],
            )

    def clear_effects(self, scope: str | None = None) -> None:
        with self._tx() as conn:
            if scope:
                conn.execute("DELETE FROM model_effects WHERE scope = ?", (scope,))
            else:
                conn.execute("DELETE FROM model_effects")
                conn.execute("DELETE FROM ghi_bias")

    def load_ghi_bias(self, source: str) -> dict[tuple[int, str], tuple[float, float]]:
        return {
            (row["hour_local"], row["horizon_bkt"]): (row["log_factor"], row["n_eff"])
            for row in self._query(
                "SELECT hour_local, horizon_bkt, log_factor, n_eff FROM ghi_bias "
                "WHERE source = ?",
                (source,),
            )
        }

    def save_ghi_bias(
        self,
        source: str,
        bias: dict[tuple[int, str], tuple[float, float]],
        updated_at: int,
    ) -> None:
        if not bias:
            return
        with self._tx() as conn:
            conn.executemany(
                """
                INSERT INTO ghi_bias
                    (source, hour_local, horizon_bkt, log_factor, n_eff, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (source, hour_local, horizon_bkt) DO UPDATE SET
                    log_factor = excluded.log_factor,
                    n_eff      = excluded.n_eff,
                    updated_at = excluded.updated_at
                """,
                [
                    (source, hour, bucket, factor, n_eff, updated_at)
                    for (hour, bucket), (factor, n_eff) in bias.items()
                ],
            )

    # -- bookkeeping ------------------------------------------------------- #

    def add_exclusion(
        self, ts_utc: int, reason: str, string_id: str = "", detail: str | None = None
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO exclusions (ts_utc, string_id, reason, detail) "
                "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (ts_utc, string_id, reason, detail),
            )

    def get_cursor(self, name: str, default: int = 0) -> int:
        rows = self._query(
            "SELECT ts_utc FROM learning_cursor WHERE name = ?", (name,)
        )
        return int(rows[0]["ts_utc"]) if rows else default

    def set_cursor(self, name: str, ts_utc: int) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO learning_cursor (name, ts_utc) VALUES (?, ?) "
                "ON CONFLICT (name) DO UPDATE SET ts_utc = excluded.ts_utc",
                (name, ts_utc),
            )

    def purge(self, before_ts: int) -> dict[str, int]:
        """Drop raw history older than the retention horizon.

        Model state, geometry and hourly aggregates are kept forever -- they are
        tiny and they are the memory of the system.
        """
        deleted: dict[str, int] = {}
        with self._tx() as conn:
            for table in (
                "string_5min",
                "weather_actual_5min",
                "plant_state_5min",
                "shading_obs",
            ):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE ts_utc < ?", (before_ts,)
                )
                deleted[table] = cursor.rowcount
            cursor = conn.execute(
                "DELETE FROM weather_forecast WHERE ts_utc < ?", (before_ts,)
            )
            deleted["weather_forecast"] = cursor.rowcount
            cursor = conn.execute(
                "DELETE FROM forecast_log WHERE ts_utc < ?", (before_ts,)
            )
            deleted["forecast_log"] = cursor.rowcount
        return deleted

    def statistics(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": str(self.path), "schema_version": SCHEMA_VERSION}
        for table in (
            "string_5min",
            "string_hourly",
            "weather_forecast",
            "weather_actual_5min",
            "plant_state_5min",
            "forecast_log",
            "shading_obs",
            "model_effects",
            "ghi_bias",
            "exclusions",
        ):
            out[table] = int(self._query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])
        try:
            out["size_bytes"] = self.path.stat().st_size
        except OSError:  # pragma: no cover - file may be gone during shutdown
            out["size_bytes"] = None
        return out


def default_value_kind() -> str:
    return VALUE_MEASURED
