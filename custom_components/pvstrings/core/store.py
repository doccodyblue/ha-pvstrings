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
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .config import INTERVAL_SECONDS, GeometrySegment

HOUR = 3600
from .quality import VALUE_MEASURED

#: Shading observations older than this are thinned to a quarter of their
#: density.  Recent sky deserves full resolution; last spring does not, and
#: the whole table is re-read on every refit.
SHADING_THIN_DAYS = 120

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 6

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

-- Measured pairs across one conversion stage, for learning its efficiency
-- curve later.  Self-contained on purpose: string_5min is raw telemetry and
-- is compacted away, these are training data and outlive it.
--
-- ``members`` records which strings fed the input *at measuring time* --
-- group membership is editable, and re-deriving it later would censor a
-- pair against strings that were not in it.  ``curtailable`` records
-- whether anything could have held this scope back, which is what makes an
-- unjudged interval readable: no limit and no battery means nothing could
-- bind, otherwise a NULL verdict is unknown and the pair is unusable.
-- ``censored`` stays NULL until physics has judged, same as limit_binding.
CREATE TABLE IF NOT EXISTS conversion_5min (
    ts_utc      INTEGER NOT NULL,
    scope_id    TEXT    NOT NULL,  -- group_id (inverter) / string_id (mppt)
    stage       TEXT    NOT NULL,  -- 'inverter' | 'mppt'
    in_w        REAL,
    out_w       REAL,
    coverage    REAL    NOT NULL,
    members     TEXT    NOT NULL,  -- comma-separated string ids
    curtailable INTEGER NOT NULL,
    censored    INTEGER,
    PRIMARY KEY (ts_utc, scope_id, stage)
);
CREATE INDEX IF NOT EXISTS ix_conversion_scope
    ON conversion_5min (scope_id, stage, ts_utc);

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
    rain_probability_pct REAL,
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

CREATE TABLE IF NOT EXISTS plant_hourly (
    ts_utc          INTEGER PRIMARY KEY,
    imported_kwh    REAL,
    exported_kwh    REAL,
    house_kwh       REAL,
    battery_soc_pct REAL
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
    -- The two nuisance-term inputs of the joint fit: the denominator's watts
    -- (so a bright string outweighs a sliver of dawn when the plant-wide
    -- moment is estimated) and the beam share of the moment's irradiance (so
    -- overcast rows cannot vote a beam shadow away).  NULL on rows written
    -- before v4.
    physics_w     REAL,
    beam          REAL,
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
        pending: int | None = None
        with self._lock:
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            self._conn.executescript(_SCHEMA)
            if current == 4:
                # v4 wrote *horizontal* beam shares; v5 stores the POA share.
                # Nulled rows take the beam_known=False path (down-weighted,
                # never inverted).  Before the version stamp, so a crash
                # cannot leave a db marked v5 with horizontal values inside.
                # Column check first: a v4 stamp without the column exists
                # when the v4 migration itself crashed mid-way.
                columns = {
                    row[1]
                    for row in self._conn.execute("PRAGMA table_info(shading_obs)")
                }
                if "beam" in columns:
                    self._conn.execute("UPDATE shading_obs SET beam = NULL")
            if current < SCHEMA_VERSION:
                _LOGGER.debug(
                    "pvstrings schema %s -> %s at %s", current, SCHEMA_VERSION, self.path
                )
                pending = current
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        # Unconditional, not gated on the version: the version stamp above is
        # written before the ALTERs run, so a crash between the two would
        # leave a database marked current with the columns missing -- and a
        # version-gated check would then never look again.  A PRAGMA per
        # connect is what the self-healing costs.
        with self._lock:
            columns = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(shading_obs)")
            }
            if "physics_w" not in columns:
                self._conn.execute(
                    "ALTER TABLE shading_obs ADD COLUMN physics_w REAL"
                )
            if "beam" not in columns:
                self._conn.execute("ALTER TABLE shading_obs ADD COLUMN beam REAL")
        if pending is not None and pending < 3:
            # ``CREATE TABLE IF NOT EXISTS`` in _SCHEMA only shapes a *new*
            # database; an existing one keeps its old columns and every insert
            # would fail on the arity.  Adding it is cheap and the old rows
            # legitimately stay NULL -- the source was never asked for it.
            with self._lock:
                columns = {
                    row[1]
                    for row in self._conn.execute(
                        "PRAGMA table_info(weather_forecast)"
                    )
                }
                if "rain_probability_pct" not in columns:
                    self._conn.execute(
                        "ALTER TABLE weather_forecast "
                        "ADD COLUMN rain_probability_pct REAL"
                    )
        if pending is not None and pending < 2:
            # plant_hourly became the source for whole-hour grid figures, and
            # those are read over the whole period since commissioning.  Without
            # this backfill an upgrading install would read zero export for all
            # of its history and its lifetime savings would jump.
            folded = self.materialise_plant_hourly()
            if folded:
                _LOGGER.info(
                    "pvstrings: folded %s existing hours into plant_hourly", folded
                )

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

    def measured_5min_range(self, start_ts: int, end_ts: int) -> list[sqlite3.Row]:
        """Clean, uncensored, well-covered intervals across every string.

        The filters live in SQL so that a plausibility check over a long window
        does not have to pull rows it will throw away.  Night needs no filter
        of its own: in the dark the power is zero, and zero production can
        never exceed a ceiling.
        """
        return self._query(
            "SELECT ts_utc, string_id, power_mean_w FROM string_5min "
            "WHERE ts_utc >= ? AND ts_utc < ? "
            "AND power_mean_w IS NOT NULL AND value_kind = ? "
            "AND coverage >= 0.8 AND COALESCE(limit_binding, 0) = 0 "
            "ORDER BY ts_utc",
            (start_ts, end_ts, VALUE_MEASURED),
        )

    def _hour_split(self, start_ts: int, end_ts: int) -> tuple[int, int]:
        """Whole-hour span inside a window, as ``(first_hour, last_hour_end)``.

        ``last_hour_end <= first_hour`` means the window contains no whole hour
        at all -- the caller must then read raw rows over the *whole* window
        and not treat the ends as two separate pieces, or a sub-hour window
        gets counted twice.
        """
        return -(-start_ts // HOUR) * HOUR, end_ts // HOUR * HOUR

    def _raw_energy_wh(
        self, start_ts: int, end_ts: int, string_id: str | None
    ) -> float:
        if end_ts <= start_ts:
            return 0.0
        sql = (
            "SELECT COALESCE(SUM(energy_wh), 0) AS wh FROM string_5min "
            "WHERE ts_utc >= ? AND ts_utc < ?"
        )
        params: list[Any] = [start_ts, end_ts]
        if string_id:
            sql += " AND string_id = ?"
            params.append(string_id)
        return float(self._query(sql, params)[0]["wh"])

    def first_hour_ts(self) -> int | None:
        """The earliest hour the plant has any measurement for.

        Not the same thing as the commissioning date, and the difference is the
        point: a plant commissioned in May and given this integration in August
        has four months of production nobody recorded.  Scaling what *was*
        recorded up to a year using the period since commissioning divides a
        week of savings by a third of a year.
        """
        rows = self._query("SELECT MIN(ts_utc) AS first FROM string_hourly")
        return None if not rows or rows[0]["first"] is None else int(rows[0]["first"])

    def energy_kwh_between(
        self, start_ts: int, end_ts: int, string_id: str | None = None
    ) -> float:
        """Produced energy in a window.

        Whole hours come from ``string_hourly`` where that row exists, so raw
        data can be discarded without the lifetime totals shrinking.  Hours
        that have *not* been folded up yet still fall back to the raw rows --
        treating a missing aggregate as zero would make today's production
        collapse whenever the learning cycle stalls, while the collector keeps
        recording perfectly good data.
        """
        first_hour, last_hour_end = self._hour_split(start_ts, end_ts)
        if last_hour_end <= first_hour:
            return self._raw_energy_wh(start_ts, end_ts, string_id) / 1000.0

        sql = (
            "SELECT ts_utc, COALESCE(SUM(energy_kwh), 0) AS kwh FROM string_hourly "
            "WHERE ts_utc >= ? AND ts_utc < ?"
        )
        params: list[Any] = [first_hour, last_hour_end]
        if string_id:
            sql += " AND string_id = ?"
            params.append(string_id)
        sql += " GROUP BY ts_utc"
        folded = {int(r["ts_utc"]): float(r["kwh"]) for r in self._query(sql, params)}

        total = sum(folded.values())
        for hour in range(first_hour, last_hour_end, HOUR):
            if hour not in folded:
                total += self._raw_energy_wh(hour, hour + HOUR, string_id) / 1000.0

        total += self._raw_energy_wh(start_ts, first_hour, string_id) / 1000.0
        total += self._raw_energy_wh(last_hour_end, end_ts, string_id) / 1000.0
        return total

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
                     rain_probability_pct, pressure_hpa, components_plausible)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (issued_at_utc, ts_utc, source) DO UPDATE SET
                    ghi_wm2              = excluded.ghi_wm2,
                    dni_wm2              = excluded.dni_wm2,
                    dhi_wm2              = excluded.dhi_wm2,
                    temp_c               = excluded.temp_c,
                    clouds_pct           = excluded.clouds_pct,
                    wind_ms              = excluded.wind_ms,
                    humidity_pct         = excluded.humidity_pct,
                    rain_mm              = excluded.rain_mm,
                    rain_probability_pct = excluded.rain_probability_pct,
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

    def weather_outlook(
        self, start_ts: int, end_ts: int, source: str
    ) -> dict[str, float | None]:
        """What the sky is expected to do over a window.

        Built on the same newest-issue-per-hour rule as :meth:`latest_forecast`
        so the outlook and the yield forecast never describe different runs.

        The rain figure is the **highest** hourly probability, not the mean:
        one hour of certain rain makes a day you plan around, and averaging it
        against twenty-three dry ones hides exactly that.  Cloud cover is a
        mean, because it is a condition rather than an event.  Rain volume is
        summed for the same reason the probability is not.

        ``None`` where the source said nothing -- an outlook of zero and an
        outlook nobody offered are different answers, and the caller has to be
        able to tell them apart.
        """
        rows = self._query(
            """
            SELECT MAX(f.rain_probability_pct) AS rain_probability_pct,
                   AVG(f.clouds_pct)           AS clouds_pct,
                   SUM(f.rain_mm)              AS rain_mm,
                   COUNT(*)                    AS hours
            FROM weather_forecast f
            JOIN (
                SELECT ts_utc, MAX(issued_at_utc) AS issued
                FROM weather_forecast
                WHERE source = ? AND ts_utc >= ? AND ts_utc < ?
                GROUP BY ts_utc
            ) latest
              ON latest.ts_utc = f.ts_utc AND latest.issued = f.issued_at_utc
            WHERE f.source = ?
            """,
            (source, start_ts, end_ts, source),
        )
        row = rows[0] if rows else None
        if row is None or not row["hours"]:
            return {"rain_probability_pct": None, "clouds_pct": None, "rain_mm": None}
        return {
            "rain_probability_pct": (
                None
                if row["rain_probability_pct"] is None
                else round(float(row["rain_probability_pct"]), 1)
            ),
            "clouds_pct": (
                None if row["clouds_pct"] is None else round(float(row["clouds_pct"]), 1)
            ),
            "rain_mm": (
                None if row["rain_mm"] is None else round(float(row["rain_mm"]), 2)
            ),
        }

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

    def materialise_plant_hourly(
        self, start_ts: int | None = None, end_ts: int | None = None
    ) -> int:
        """Fold plant state into hourly energies so the raw rows can go.

        Import and export are accumulated separately rather than netted: a
        house drawing 500 W for half an hour and exporting 500 W for the other
        half nets to zero and has two very different tariff outcomes.

        With no bounds it folds everything present, which is what the schema
        migration needs -- an install upgrading from schema 1 has months of
        plant state and no aggregate, and reading that as zero export would
        silently rewrite its lifetime savings.
        """
        where, params = "", []
        if start_ts is not None:
            where += " AND ts_utc >= ?"
            params.append(start_ts // HOUR * HOUR)
        if end_ts is not None:
            where += " AND ts_utc < ?"
            params.append(end_ts)
        hours_ = INTERVAL_SECONDS / 3600.0 / 1000.0
        with self._tx() as conn:
            cursor = conn.execute(
                f"""
                INSERT INTO plant_hourly
                    (ts_utc, imported_kwh, exported_kwh, house_kwh, battery_soc_pct)
                SELECT ts_utc / {HOUR} * {HOUR} AS hour,
                       SUM(CASE WHEN grid_power_w > 0 THEN grid_power_w ELSE 0 END) * ?,
                       -SUM(CASE WHEN grid_power_w < 0 THEN grid_power_w ELSE 0 END) * ?,
                       SUM(COALESCE(house_load_w, 0)) * ?,
                       AVG(battery_soc_pct)
                  FROM plant_state_5min
                 WHERE 1=1{where}
                 GROUP BY hour
                ON CONFLICT (ts_utc) DO UPDATE SET
                    imported_kwh    = excluded.imported_kwh,
                    exported_kwh    = excluded.exported_kwh,
                    house_kwh       = excluded.house_kwh,
                    battery_soc_pct = excluded.battery_soc_pct
                """,
                (hours_, hours_, hours_, *params),
            )
            return cursor.rowcount

    def battery_soc_series(self, start_ts: int, end_ts: int) -> dict[int, float]:
        """Battery state of charge per five-minute interval.

        Used to tell a throttled interval from a dim one: once the battery is
        full there is a known mechanism holding the strings back, and a
        measurement taken through it is a lower bound rather than a reading.
        """
        return {
            int(row["ts_utc"]): float(row["battery_soc_pct"])
            for row in self._query(
                "SELECT ts_utc, battery_soc_pct FROM plant_state_5min "
                "WHERE ts_utc >= ? AND ts_utc < ? AND battery_soc_pct IS NOT NULL",
                (start_ts, end_ts),
            )
        }

    def _raw_grid_kwh(self, start_ts: int, end_ts: int) -> tuple[float, float]:
        if end_ts <= start_ts:
            return 0.0, 0.0
        rows = self._query(
            "SELECT grid_power_w FROM plant_state_5min "
            "WHERE ts_utc >= ? AND ts_utc < ? AND grid_power_w IS NOT NULL",
            (start_ts, end_ts),
        )
        scale = INTERVAL_SECONDS / 3600.0 / 1000.0
        return (
            sum(r["grid_power_w"] for r in rows if r["grid_power_w"] > 0) * scale,
            -sum(r["grid_power_w"] for r in rows if r["grid_power_w"] < 0) * scale,
        )

    def grid_energy_kwh(self, start_ts: int, end_ts: int) -> tuple[float, float]:
        """Imported and exported kWh, hourly where the aggregate exists."""
        first_hour, last_hour_end = self._hour_split(start_ts, end_ts)
        if last_hour_end <= first_hour:
            return self._raw_grid_kwh(start_ts, end_ts)

        rows = self._query(
            "SELECT ts_utc, imported_kwh, exported_kwh FROM plant_hourly "
            "WHERE ts_utc >= ? AND ts_utc < ?",
            (first_hour, last_hour_end),
        )
        folded = {int(r["ts_utc"]): r for r in rows}
        imported = sum(float(r["imported_kwh"] or 0.0) for r in rows)
        exported = sum(float(r["exported_kwh"] or 0.0) for r in rows)

        for hour in range(first_hour, last_hour_end, HOUR):
            if hour not in folded:
                i, e = self._raw_grid_kwh(hour, hour + HOUR)
                imported += i
                exported += e

        for lo, hi in ((start_ts, first_hour), (last_hour_end, end_ts)):
            i, e = self._raw_grid_kwh(lo, hi)
            imported += i
            exported += e
        return imported, exported

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

    #: The two pairings below differ only in how the cut-off is expressed, so
    #: they share their SQL.  ``{cutoff}`` is either an offset from the target
    #: hour or a literal timestamp; everything else -- the columns, the
    #: hindsight-proof ``<=``, the newest-issue-wins ordering -- has to stay
    #: identical, or the two scores would stop being comparable.
    _FORECAST_VS_ACTUAL_SQL = """
        SELECT h.ts_utc, h.string_id, h.energy_kwh, h.quality, h.value_kind,
               h.curtailed_fraction, h.coverage,
               (SELECT f.potential_kwh FROM forecast_log f
                 WHERE f.ts_utc = h.ts_utc
                   AND f.string_id = h.string_id
                   AND f.issued_at_utc <= {cutoff}
                 ORDER BY f.issued_at_utc DESC LIMIT 1) AS potential_kwh
        FROM string_hourly h
        WHERE h.ts_utc >= ? AND h.ts_utc < ?
        ORDER BY h.ts_utc
    """

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
            self._FORECAST_VS_ACTUAL_SQL.format(cutoff="h.ts_utc - ?"),
            (lead_seconds, start_ts, end_ts),
        )

    def forecast_vs_actual_before(
        self, start_ts: int, end_ts: int, issued_before_ts: int
    ) -> list[sqlite3.Row]:
        """Pair measured hours with the forecast as it stood at one moment.

        The sibling above asks "how far ahead was this issued", which lets
        different hours of the same day come from different model runs.  This
        one pins a single instant -- in practice the evening before -- so a
        whole day is scored against one coherent run, and against exactly the
        numbers somebody would have read off the dashboard at that time.

        A missing issue at that instant is not an error: the ordering falls
        back to the newest run before it, which is what the reader would have
        seen too.
        """
        return self._query(
            self._FORECAST_VS_ACTUAL_SQL.format(cutoff="?"),
            (issued_before_ts, start_ts, end_ts),
        )

    # -- shading ----------------------------------------------------------- #

    def add_shading_obs(self, rows: Iterable[tuple[Any, ...]]) -> None:
        # Rows written before the joint fit existed carry six fields; pad them
        # so one INSERT serves both writers instead of forking the statement.
        payload = [
            tuple(row) + (None,) * (8 - len(row)) for row in rows
        ]
        if not payload:
            return
        with self._tx() as conn:
            conn.executemany(
                """
                INSERT INTO shading_obs
                    (ts_utc, string_id, azimuth_deg, elevation_deg, ratio,
                     weight, physics_w, beam)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (ts_utc, string_id) DO UPDATE SET
                    azimuth_deg   = excluded.azimuth_deg,
                    elevation_deg = excluded.elevation_deg,
                    ratio         = excluded.ratio,
                    weight        = excluded.weight,
                    physics_w     = excluded.physics_w,
                    beam          = excluded.beam
                """,
                payload,
            )

    def shading_rows_by_string(
        self,
    ) -> dict[str, list[tuple[Any, ...]]]:
        """Every usable observation, grouped by string, for a map refit.

        Returned as plain tuples rather than rows: the fitter is pure and must
        stay testable without a database behind it.  The trailing two fields
        are ``None`` on rows written before schema v4.
        """
        rows = self._query(
            "SELECT ts_utc, string_id, azimuth_deg, elevation_deg, ratio, "
            "weight, physics_w, beam "
            "FROM shading_obs ORDER BY string_id",
            (),
        )
        grouped: dict[str, list[tuple[Any, ...]]] = {}
        for row in rows:
            grouped.setdefault(row["string_id"], []).append(
                (
                    float(row["ts_utc"]),
                    float(row["azimuth_deg"]),
                    float(row["elevation_deg"]),
                    float(row["ratio"]),
                    float(row["weight"]),
                    None if row["physics_w"] is None else float(row["physics_w"]),
                    None if row["beam"] is None else float(row["beam"]),
                )
            )
        return grouped

    def clear_shading_obs(self, string_id: str | None = None) -> int:
        """Forget shading observations -- one string's, or every string's.

        Part of resetting the model, not a maintenance chore: the map is a
        learned correction, and leaving it in place while the per-string
        effects that offset it are wiped leaves the forecast worse than either
        state on its own.  It is also the only way back from a backfill that
        turned out to be built on a mis-scaled sensor.  Per string because
        ratios are frozen against the geometry at collect time: correcting one
        string's geometry poisons only that string's rows.
        """
        with self._tx() as conn:
            if string_id:
                cur = conn.execute(
                    "DELETE FROM shading_obs WHERE string_id = ?", (string_id,)
                )
            else:
                cur = conn.execute("DELETE FROM shading_obs")
            return cur.rowcount

    def clear_effects_for_string(self, string_id: str) -> None:
        """One string's learned factors; plant scope and ghi_bias stay.

        Key shapes owned by the learning layer: scope "string" keys on the
        bare id, "string_daypart" on ``id|part``.
        """
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM model_effects WHERE scope = 'string' AND key = ?",
                (string_id,),
            )
            conn.execute(
                "DELETE FROM model_effects "
                "WHERE scope = 'string_daypart' AND key LIKE ? || '|%'",
                (string_id,),
            )

    def shading_observations_by_string(self) -> dict[str, int]:
        """Counts in one query -- a dashboard should not cost one per string."""
        return {
            row["string_id"]: int(row["n"])
            for row in self._query(
                "SELECT string_id, COUNT(*) AS n FROM shading_obs GROUP BY string_id",
                (),
            )
        }

    # -- conversion stages --------------------------------------------------- #

    def upsert_conversion(self, rows: Iterable[tuple[Any, ...]]) -> None:
        """``(ts, scope_id, stage, in_w, out_w, coverage, members, curtailable)``.

        ``censored`` is left alone on conflict: physics stamps it later and a
        re-flush of the same interval must not undo that verdict.
        """
        payload = list(rows)
        if not payload:
            return
        with self._tx() as conn:
            conn.executemany(
                """
                INSERT INTO conversion_5min
                    (ts_utc, scope_id, stage, in_w, out_w, coverage,
                     members, curtailable)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (ts_utc, scope_id, stage) DO UPDATE SET
                    in_w        = excluded.in_w,
                    out_w       = excluded.out_w,
                    coverage    = excluded.coverage,
                    members     = excluded.members,
                    curtailable = excluded.curtailable
                """,
                payload,
            )

    def mark_conversion_censored(self, start_ts: int, end_ts: int) -> int:
        """Carry the physics verdict over onto the conversion pairs.

        An interval in which any contributing string was held back says
        nothing about conversion efficiency -- the output followed a limit,
        not the input.  Judged after the fact because the collector cannot
        know it at write time.

        Membership comes from the row's own ``members``, not from current
        configuration: a group edited between measuring and learning would
        otherwise be censored against strings that never fed it.

        A pair is unusable unless every contributing string is a clean,
        judged measurement.  ``limit_binding IS NULL`` means physics has not
        decided, which is only harmless where nothing could bind anyway --
        hence the ``curtailable`` test rather than treating NULL as "free".
        """
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE conversion_5min SET censored = COALESCE((
                    SELECT CASE
                        -- Every contributing string must be present and
                        -- clean; a missing row is missing evidence, and a
                        -- partial input against a full output would read as
                        -- an efficiency the stage never had.
                        WHEN COUNT(*) < (
                            length(conversion_5min.members)
                            - length(replace(conversion_5min.members, ',', ''))
                            + 1
                        ) THEN 1
                        ELSE MAX(CASE
                            WHEN s.value_kind <> 'measured' THEN 1
                            WHEN s.limit_binding = 1 THEN 1
                            WHEN s.limit_binding IS NULL
                                 AND (conversion_5min.curtailable = 1
                                      OR s.limit_commanded_w IS NOT NULL) THEN 1
                            ELSE 0 END)
                    END
                      FROM string_5min s
                     WHERE s.ts_utc = conversion_5min.ts_utc
                       AND instr(
                           ',' || conversion_5min.members || ',',
                           ',' || s.string_id || ','
                       ) > 0
                ), 1)
                WHERE ts_utc >= ? AND ts_utc < ?
                """,
                (start_ts, end_ts),
            )
            return cur.rowcount

    def conversion_rows(
        self, scope_id: str | None = None, uncensored_only: bool = True
    ) -> list[tuple[Any, ...]]:
        """Training pairs, newest last.  The fit consumes these (phase 4b)."""
        # in_w > 0 belt-and-braces: the collector already refuses loadless
        # intervals, but a fit dividing by this must never see a zero.
        sql = (
            "SELECT ts_utc, scope_id, stage, in_w, out_w, coverage, censored "
            "FROM conversion_5min "
            "WHERE in_w > 0 AND out_w IS NOT NULL AND out_w >= 0"
        )
        params: list[Any] = []
        if uncensored_only:
            sql += " AND COALESCE(censored, 1) = 0"
        if scope_id:
            sql += " AND scope_id = ?"
            params.append(scope_id)
        sql += " ORDER BY ts_utc"
        return [
            (
                int(r["ts_utc"]),
                r["scope_id"],
                r["stage"],
                float(r["in_w"]),
                float(r["out_w"]),
                float(r["coverage"]),
                r["censored"],
            )
            for r in self._query(sql, params)
        ]

    def conversion_counts(
        self, expected: Iterable[str] = ()
    ) -> dict[str, dict[str, int]]:
        """Per scope: how much usable evidence has accumulated so far.

        ``expected`` seeds the configured scopes at zero so a stage that is
        set up but collecting nothing is visible.  Without it an absent key
        is indistinguishable from a stage nobody configured -- and a silent
        gap in training data is the one failure this table exists to avoid.
        """
        out: dict[str, dict[str, int]] = {
            key: {"rows": 0, "usable": 0} for key in expected
        }
        for row in self._query(
            "SELECT scope_id, stage, COUNT(*) AS n, "
            "SUM(CASE WHEN COALESCE(censored, 1) = 0 THEN 1 ELSE 0 END) AS usable "
            "FROM conversion_5min GROUP BY scope_id, stage",
            (),
        ):
            out[f"{row['scope_id']}|{row['stage']}"] = {
                "rows": int(row["n"]),
                "usable": int(row["usable"] or 0),
            }
        return out

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

    def compact(
        self,
        now_ts: int,
        raw_days: int = 90,
        #: Forecast issues have to outlive the longest window they are scored
        #: over, or day-ahead accuracy quietly degrades: past this horizon only
        #: the newest issue per hour survives, which is the *nowcast*, and an
        #: older-issue lookup then finds nothing and drops the hour from the
        #: score without saying so.  The caller passes the value derived from
        #: its score windows; this default only has to be safe on its own.
        issue_days: int = 35,
        exclusion_days: int = 90,
        shading_days: int = 730,
    ) -> dict[str, int]:
        """Tiered retention: condense first, then discard.

        One blanket horizon over every table is the wrong shape here. The
        aggregates are small and are the memory of the system; the raw rows are
        large and stop being useful once they have been folded up. And the
        forecast tables are dominated not by target hours but by *issues* --
        the same hour re-forecast every half hour -- of which only the newest
        matters once its verification window has passed.

        Raw rows are only dropped where the corresponding aggregate row exists,
        so compaction can never quietly shrink a lifetime total.
        """
        raw_cut = now_ts - raw_days * 86400
        issue_cut = now_ts - issue_days * 86400
        deleted: dict[str, int] = {}

        with self._tx() as conn:
            # Five-minute measurements: only where the hour was folded up.
            cur = conn.execute(
                "DELETE FROM string_5min WHERE ts_utc < ? AND EXISTS ("
                "  SELECT 1 FROM string_hourly h WHERE h.string_id = string_5min.string_id"
                "    AND h.ts_utc = string_5min.ts_utc / 3600 * 3600)",
                (raw_cut,),
            )
            deleted["string_5min"] = cur.rowcount

            cur = conn.execute(
                "DELETE FROM plant_state_5min WHERE ts_utc < ? AND EXISTS ("
                "  SELECT 1 FROM plant_hourly p"
                "   WHERE p.ts_utc = plant_state_5min.ts_utc / 3600 * 3600)",
                (raw_cut,),
            )
            deleted["plant_state_5min"] = cur.rowcount

            cur = conn.execute(
                "DELETE FROM weather_actual_5min WHERE ts_utc < ?", (raw_cut,)
            )
            deleted["weather_actual_5min"] = cur.rowcount

            # Forecast issues: past the verification window keep only the run
            # that came closest to the target hour -- the best estimate of what
            # the irradiance actually was.
            # Keep the run closest to the target hour -- the best estimate of
            # what the irradiance actually was.  Ordering by ABS() rather than
            # filtering to horizon >= 0 matters: an hour that was already past
            # when it was first fetched has *only* negative horizons, the
            # subquery would return NULL, "<> NULL" is NULL, and every row for
            # that hour would survive for ever.
            cur = conn.execute(
                "DELETE FROM weather_forecast WHERE ts_utc < ? AND issued_at_utc <> ("
                "  SELECT f.issued_at_utc FROM weather_forecast f"
                "   WHERE f.ts_utc = weather_forecast.ts_utc"
                "     AND f.source = weather_forecast.source"
                "   ORDER BY ABS(f.horizon_h) ASC, f.issued_at_utc DESC LIMIT 1)",
                (issue_cut,),
            )
            deleted["weather_forecast_issues"] = cur.rowcount

            # Forecast log: the same, keeping the last issue before the hour.
            cur = conn.execute(
                "DELETE FROM forecast_log WHERE ts_utc < ? AND issued_at_utc <> ("
                "  SELECT f.issued_at_utc FROM forecast_log f"
                "   WHERE f.ts_utc = forecast_log.ts_utc"
                "     AND f.string_id = forecast_log.string_id"
                "   ORDER BY f.issued_at_utc DESC LIMIT 1)",
                (issue_cut,),
            )
            deleted["forecast_log_issues"] = cur.rowcount

            cur = conn.execute(
                "DELETE FROM exclusions WHERE ts_utc < ?",
                (now_ts - exclusion_days * 86400,),
            )
            deleted["exclusions"] = cur.rowcount

            # Shading observations are raw material for an analysis that needs
            # a year of them, so they get a long horizon of their own rather
            # than the raw one -- but not an unbounded life.
            cur = conn.execute(
                "DELETE FROM shading_obs WHERE ts_utc < ?",
                (now_ts - shading_days * 86400,),
            )
            # Beyond a season, thin what survives.  A sky cell is described
            # just as well by every fourth interval of an old afternoon as by
            # all twelve, and keeping all of them grows the table -- which is
            # re-read in full on every refit -- without improving the map.
            #
            # Only the five-minute grid.  Backfilled rows are already one per
            # hour, a twelfth of the live density, so there is nothing in them
            # to thin -- and they sit deliberately one second off the grid, so
            # every one of them lands on the same residue and a blanket rule
            # would delete the lot rather than three quarters of it.
            cur2 = conn.execute(
                "DELETE FROM shading_obs WHERE ts_utc < ? "
                "AND ts_utc % 300 = 0 AND (ts_utc / 300) % 4 != 0",
                (now_ts - SHADING_THIN_DAYS * 86400,),
            )
            deleted["shading_obs"] = cur.rowcount + cur2.rowcount

            # Conversion pairs are training data, not telemetry: they follow
            # the shading horizon, not the raw one, and are never dropped
            # just because string_5min was compacted -- they are complete on
            # their own precisely so that can happen.
            cur = conn.execute(
                "DELETE FROM conversion_5min WHERE ts_utc < ?",
                (now_ts - shading_days * 86400,),
            )
            deleted["conversion_5min"] = cur.rowcount

            # Backstop: nothing may outlive every rule above.  The condensing
            # rules keep one row per target hour indefinitely, which is the
            # point, but only within a horizon.
            hard_cut = now_ts - max(shading_days, 2 * 365) * 86400
            for table in ("weather_forecast", "forecast_log"):
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE ts_utc < ?", (hard_cut,)
                )
                deleted[f"{table}_expired"] = cur.rowcount

        return deleted

    def vacuum(self) -> None:
        """Return freed pages to the filesystem.  Blocking; call rarely."""
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        with self._lock:
            self._conn.execute("VACUUM")

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
