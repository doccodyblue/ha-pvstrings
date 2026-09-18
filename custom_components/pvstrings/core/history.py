"""Looking back: one past day in full, and accuracy week by week.

Read by two response-only services.  Nothing here is published as state, so a
day of five-minute power or a year of weeks never reaches the recorder.

Every number has to be what the dashboard showed at the time or what the
scores used -- the pairing and the censoring rule are the score's own, not
re-implementations of them.

Weeks are persisted when they close, because the day-ahead issues they are
scored against only outlive the score window by a few days.  A week that was
not closed while its issues existed is missing, not invented.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .config import INTERVAL_SECONDS
from .forecast import (
    CURSOR_LEARN,
    DAY_AHEAD_ISSUE_HOUR_LOCAL,
    HOUR,
    _hourly_profile,
    _ScoreTally,
    is_uncensored,
)

if TYPE_CHECKING:
    from .forecast import ForecastEngine

_LOGGER = logging.getLogger(__name__)

RESPONSE_VERSION = 1
#: Shape of the JSON stored in ``accuracy_weekly.payload``.  Bumped when a
#: stored week can no longer be read as a current one.
WEEK_PAYLOAD_VERSION = 2
#: Versions the reader still understands.  A week is written once and never
#: rescored, so an older payload must stay readable -- it is simply reported
#: with what it can prove about itself, which for version 1 is nothing about
#: where its baseline came from.
WEEK_PAYLOAD_VERSIONS = (1, 2)

#: Where a week's baseline figure comes from, which decides whether the week
#: may carry a causal claim at all.
BASIS_LIVE = "live"          # every paired hour's baseline was logged live
BASIS_REPLAYED = "replayed"  # every one was reconstructed with today's code
BASIS_MIXED = "mixed"        # some of each -- not one comparison, two
BASIS_UNKNOWN = "unknown"    # a version-1 row: it cannot say
#: Hours the live coordinator forecasts per run.  A replayed baseline has to
#: use the live window: ``physics.run`` decides one components flag over the
#: whole series, so a shorter window can change every hour in it.
LIVE_WINDOW_HOURS = 72
#: A week closed later than this after its end gets no model snapshot: the
#: model has moved on, and a snapshot of today labelled with that week would
#: overstate how mature it was.
MATURITY_MAX_LAG_S = 2 * 86400


def _local_midnight(day: date, tz: Any) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=tz).timestamp())


def _iso(ts_utc: int, tz: Any) -> str:
    return datetime.fromtimestamp(ts_utc, tz=tz).isoformat()


def _local_date(ts_utc: int | None, tz: Any) -> str | None:
    if ts_utc is None:
        return None
    return datetime.fromtimestamp(ts_utc, tz=tz).date().isoformat()


def _r(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


# --------------------------------------------------------------------------- #
# one day
# --------------------------------------------------------------------------- #


def day_payload(engine: "ForecastEngine", day: date) -> dict[str, Any]:
    """Everything the forecast card needs to redraw a past day."""
    store, tz = engine.store, engine._tz
    start = _local_midnight(day, tz)
    end = _local_midnight(day + timedelta(days=1), tz)
    cutoff = engine.day_ahead_cutoff(start)

    actual = {(row.ts_utc, row.string_id): row for row in store.hourly_range(start, end)}
    lead0 = {
        (row["ts_utc"], row["string_id"]): row
        for row in store.forecast_log_as_of(start, end)
    }
    ahead = {
        (row["ts_utc"], row["string_id"]): row
        for row in store.forecast_log_as_of(start, end, cutoff)
    }

    raw = store.fivemin_energy(start, end)
    configured = [string.string_id for string in engine.plant.strings]
    seen = {key[1] for key in (*actual, *lead0)}
    string_ids = configured + sorted(seen - set(configured))

    strings: dict[str, Any] = {}
    plant_hours: dict[int, dict[str, Any]] = {}
    for string_id in string_ids:
        hours = sorted({ts for ts, sid in (*actual, *lead0) if sid == string_id})
        out: list[dict[str, Any]] = []
        for ts in hours:
            fc = lead0.get((ts, string_id))
            da = ahead.get((ts, string_id))
            act = actual.get((ts, string_id))
            censored = (
                None
                if act is None or act.energy_kwh is None
                else not is_uncensored(act.value_kind, act.curtailed_fraction)
            )
            entry = {
                "start": _iso(ts, tz),
                "forecast_kwh": _r(fc["potential_kwh"]) if fc else None,
                "day_ahead_kwh": _r(da["potential_kwh"]) if da else None,
                "day_ahead_issued_at": _iso(da["issued_at_utc"], tz) if da else None,
                "baseline_kwh": _r(fc["baseline_kwh"]) if fc else None,
                "unshaded_kwh": _r(fc["unshaded_kwh"]) if fc else None,
                "actual_kwh": _r(act.energy_kwh) if act else None,
                "censored": bool(censored),
                "value_kind": act.value_kind if act else None,
                "curtailed_fraction": _r(act.curtailed_fraction) if act else None,
                "coverage": _r(act.coverage) if act else None,
            }
            out.append(entry)
            _fold_plant_hour(plant_hours.setdefault(ts, {"start": entry["start"]}), entry)
        strings[string_id] = {
            "hours": out,
            "intervals": _intervals(raw, start, end, tz, string_id),
        }

    first_hourly = store.first_ts("string_hourly")
    first_5min = store.first_ts("string_5min")
    return {
        "version": RESPONSE_VERSION,
        "date": day.isoformat(),
        "earliest_date": _local_date(first_hourly, tz),
        "day_ahead_since": _local_date(store.first_multi_issue_ts(), tz),
        "intervals_since": _local_date(first_5min, tz),
        "issue_hour_local": DAY_AHEAD_ISSUE_HOUR_LOCAL,
        "plant": {
            "hours": [_finish_plant_hour(plant_hours[ts]) for ts in sorted(plant_hours)],
            "intervals": _intervals(raw, start, end, tz, None),
        },
        "strings": strings,
    }


_SUMMED = ("forecast_kwh", "day_ahead_kwh", "baseline_kwh", "unshaded_kwh", "actual_kwh")


def _fold_plant_hour(bucket: dict[str, Any], entry: Mapping[str, Any]) -> None:
    """Sum over the strings that have a value; ``None`` only if none has one."""
    for key in _SUMMED:
        if entry[key] is not None:
            bucket[key] = bucket.get(key, 0.0) + entry[key]
    bucket["censored"] = bucket.get("censored", False) or entry["censored"]
    # Which run it was is only meaningful when every string agrees; the
    # newest one is what a reader of the whole plant saw at the latest.
    issued = entry["day_ahead_issued_at"]
    if issued is not None and (
        bucket.get("day_ahead_issued_at") is None or issued > bucket["day_ahead_issued_at"]
    ):
        bucket["day_ahead_issued_at"] = issued


def _finish_plant_hour(bucket: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": bucket["start"],
        **{key: _r(bucket.get(key)) for key in _SUMMED[:4]},
        "day_ahead_issued_at": bucket.get("day_ahead_issued_at"),
        "actual_kwh": _r(bucket.get("actual_kwh")),
        "censored": bool(bucket.get("censored", False)),
    }


def _intervals(
    raw: list[Any], start: int, end: int, tz: Any, string_id: str | None
) -> dict[str, Any]:
    """Five-minute power as a dense array from the day's start.

    A missing interval is ``None``, never zero: compaction removes raw rows
    once their hour is folded, and a gap is not a dark sky.  A day with no raw
    rows at all returns an empty array, which is how the dashboard tells
    "compacted away" from "measured nothing".
    """
    slots = (end - start) // INTERVAL_SECONDS
    power: list[float | None] = [None] * slots
    found = False
    for row in raw:
        if string_id is not None and row["string_id"] != string_id:
            continue
        if row["energy_wh"] is None:
            continue
        index = (int(row["ts_utc"]) - start) // INTERVAL_SECONDS
        if 0 <= index < slots:
            watts = float(row["energy_wh"]) * 3600 / INTERVAL_SECONDS
            power[index] = (power[index] or 0.0) + watts
            found = True
    return {
        "start": _iso(start, tz),
        "step_minutes": INTERVAL_SECONDS // 60,
        "power_w": [None if w is None else round(w, 1) for w in power] if found else [],
    }


# --------------------------------------------------------------------------- #
# weeks
# --------------------------------------------------------------------------- #


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_bounds(monday: date, tz: Any) -> tuple[int, int]:
    """Local Monday 00:00 to the next local Monday 00:00, as UTC.

    From calendar dates, so the two weeks a year with a DST switch are 167 and
    169 hours long, like the week a reader sees.
    """
    return _local_midnight(monday, tz), _local_midnight(monday + timedelta(days=7), tz)


def maturity_snapshot(engine: "ForecastEngine") -> dict[str, Any]:
    """How much the model knew: evidence per plant bucket, pooled sky cells per string."""
    return {
        "weather_n_eff": {
            key: round(effect.n_eff, 2)
            for key, effect in sorted(engine.model.plant.items())
        },
        "sky_cells": {
            string_id: len(sky.cells)
            for string_id, sky in sorted(engine.shading.maps.items())
        },
    }


def _block(
    daily: Mapping[str, list[float]],
    pairs: Sequence[tuple[float, float]],
) -> dict[str, Any]:
    """One series over one hour set: its sums, and its error two ways.

    ``abs_error_kwh`` is the absolute error of the daily *net*, which is what
    the day-ahead score reports -- an hour early and an hour late cancel
    inside the day.  That cancellation is exactly why it cannot answer "did
    learning help": a run without a multiplicative correction cancels more
    readily than one with it.  ``abs_error_hourly_kwh`` is therefore published
    next to it, summed per hour, and it is the figure a comparison uses.
    """
    forecast = sum(values[0] for values in daily.values())
    actual = sum(values[1] for values in daily.values())
    return {
        "forecast_kwh": round(forecast, 3),
        "actual_kwh": round(actual, 3),
        "abs_error_kwh": round(
            sum(abs(values[0] - values[1]) for values in daily.values()), 3
        ),
        "abs_error_hourly_kwh": round(
            sum(abs(predicted - real) for predicted, real in pairs), 3
        ),
        # What this block actually rests on, which is not the week's size:
        # only hours with a baseline can be compared, and saying so is the
        # difference between a thin week and a good one.
        "hours": len(pairs),
        "days": len(daily),
    }


def week_payload(
    engine: "ForecastEngine",
    monday: date,
    now_ts: int,
    maturity: dict[str, Any] | None,
    replay: bool = False,
) -> dict[str, Any]:
    """Day-ahead accuracy of one local week, as sums the dashboard can add up.

    Only complete local days count, exactly as in the day-ahead score.  The
    rows are the score's own pairing against each day's evening cut-off.

    ``baseline`` is the same pairing with ``baseline_kwh`` in place of the
    published figure.  When the week has one, ``day_ahead`` is computed over
    the same hours only, or a learning gain would compare different hours;
    without one, ``day_ahead`` is the full score.

    ``replay`` rebuilds a baseline the log does not carry -- rows written
    before it existed -- from the weather issue the published figure was
    computed from, while that issue still exists.  Only from the coordinator's
    own update: a replay runs the engine, which saves and restores the live
    nowcast state, and doing that from a service thread next to a live run
    could put a stale nowcast back.
    """
    tz = engine._tz
    today = datetime.fromtimestamp(now_ts, tz=tz).date()
    full = _ScoreTally()
    paired_published = _ScoreTally()
    paired_baseline = _ScoreTally()
    replayed = 0
    paired = 0

    for offset in range(7):
        day = monday + timedelta(days=offset)
        if day >= today:
            break
        start = _local_midnight(day, tz)
        end = _local_midnight(day + timedelta(days=1), tz)
        cutoff = engine.day_ahead_cutoff(start)
        rows = [dict(row) for row in engine.store.forecast_vs_actual_before(start, end, cutoff)]
        if replay:
            replayed += _replay_baseline(engine, rows)
        engine._tally(rows, full, detailed=True)
        with_baseline = [row for row in rows if row["baseline_kwh"] is not None]
        paired += len(with_baseline)
        engine._tally(with_baseline, paired_published)
        engine._tally(
            [{**row, "potential_kwh": row["baseline_kwh"]} for row in with_baseline],
            paired_baseline,
        )

    has_baseline = bool(paired_baseline.uncensored)
    if has_baseline:
        day_ahead = _block(paired_published.daily_uncensored, paired_published.uncensored)
        baseline = _block(paired_baseline.daily_uncensored, paired_baseline.uncensored)
    else:
        day_ahead = _block(full.daily_uncensored, full.uncensored)
        baseline = None
    return {
        # Where the baseline came from, per week and not per batch: a week
        # can hold both kinds, and then it is two comparisons rather than
        # one.  Only a live week may carry a causal claim.
        "baseline_basis": (
            None
            if not has_baseline
            else BASIS_LIVE
            if not replayed
            else BASIS_REPLAYED
            if replayed >= paired
            else BASIS_MIXED
        ),
        "days_scored": len(full.daily_all),
        "hours_scored": len(full.every),
        "hours_uncensored": len(full.uncensored),
        "day_ahead": day_ahead,
        "baseline": baseline,
        # All paired hours, like the attribute of the 30-day sensor.
        "hourly_profile": _hourly_profile(full),
        "maturity": maturity,
    }


def _replay_baseline(engine: "ForecastEngine", rows: list[dict[str, Any]]) -> int:
    """Fill missing ``baseline_kwh`` in place from a replay of the issue's weather.

    Returns how many rows were filled, which is what tells a week whether its
    comparison was live, reconstructed, or both.

    Grouped by issue: every hour of a day normally comes from the one evening
    run.  The weather cut-off is the logged issue instant itself: the issue is
    quantised to its hour, so a later cut-off would let the reconstruction
    read a run the original never had -- an hour of hindsight that only
    reconstructed weeks would carry, which is precisely the bias that makes
    them incomparable.  Geometry and configuration are still today's, which
    is what the basis tells the reader.
    """
    missing: dict[int, list[dict[str, Any]]] = {}
    filled = 0
    for row in rows:
        if row["baseline_kwh"] is None and row["issued_at_utc"] is not None:
            missing.setdefault(int(row["issued_at_utc"]), []).append(row)
    tz = engine._tz
    for issued, group in missing.items():
        run_day = datetime.fromtimestamp(issued, tz=tz).date()
        replayed = engine.baseline_forecast(
            issued,
            hours=LIVE_WINDOW_HOURS,
            start_ts=_local_midnight(run_day, tz),
            weather_issued_before=issued,
        )
        for row in group:
            value = replayed.get((int(row["ts_utc"]), str(row["string_id"])))
            if value is not None:
                row["baseline_kwh"] = value
                filled += 1
    return filled


def close_weeks(
    engine: "ForecastEngine", now_ts: int, issue_days: int
) -> int:
    """Persist every closed week whose issues still exist.  Returns rows written.

    Safe to call every cycle and after any restart: the table only ever gains
    rows, and a week already there is left alone.

    A week is closed only once the learning cursor has passed its end: until
    then its last hours are not folded and curtailment verdicts may still
    change them.  Only weeks whose first day is at least a day inside the
    issue window are taken -- compaction runs daily, and a week whose Monday
    issues are about to go would be scored on the hours that happen to be
    left.

    An empty table means this is the first run after the update: those weeks
    are closed as ``backfilled`` with no model snapshot.
    """
    store, tz = engine.store, engine._tz
    ready_until = min(now_ts, store.get_cursor(CURSOR_LEARN, default=0))
    last_end = store.last_week_end()
    backfilling = last_end is None
    oldest_start = now_ts - (issue_days - 1) * 86400

    monday = monday_of(datetime.fromtimestamp(now_ts, tz=tz).date())
    candidates: list[date] = []
    while True:
        monday -= timedelta(days=7)
        start, end = week_bounds(monday, tz)
        if start < oldest_start or (last_end is not None and end <= last_end):
            break
        if end <= ready_until:
            candidates.append(monday)

    written = 0
    for monday in reversed(candidates):
        start, end = week_bounds(monday, tz)
        fresh = not backfilling and now_ts - end <= MATURITY_MAX_LAG_S
        payload = week_payload(
            engine,
            monday,
            now_ts,
            maturity_snapshot(engine) if fresh else None,
            replay=True,
        )
        if not payload["days_scored"]:
            continue
        if store.insert_week(
            monday.isoformat(),
            start,
            end,
            now_ts,
            backfilling,
            WEEK_PAYLOAD_VERSION,
            json.dumps(payload, separators=(",", ":")),
        ):
            written += 1
    if written:
        _LOGGER.info(
            "pvstrings: closed %s accuracy week(s)%s",
            written,
            " (backfilled)" if backfilling else "",
        )
    return written


def weeks_payload(engine: "ForecastEngine", now_ts: int) -> dict[str, Any]:
    """Every stored week, then whatever has not been closed yet, computed live.

    Normally that is only the running week.  For the hour or so after Monday
    midnight the week before is not closed either -- its last hours wait for
    the learning cursor -- and leaving it out would show a gap that fills
    itself an hour later.  Live rows never replay a baseline (see
    :func:`week_payload`).
    """
    weeks: list[dict[str, Any]] = []
    for row in engine.store.weeks():
        if row["payload_version"] not in WEEK_PAYLOAD_VERSIONS:
            continue
        payload = json.loads(row["payload"])
        # A version-1 week was written before the basis was recorded, and its
        # payload holds sums only -- nothing in it can prove where its
        # baseline came from.  It says so rather than claiming to be live.
        if payload.get("baseline") is not None and "baseline_basis" not in payload:
            payload["baseline_basis"] = BASIS_UNKNOWN
        weeks.append(
            {
                "week_start": row["week_start"],
                "complete": True,
                "backfilled": bool(row["backfilled"]),
                **payload,
            }
        )
    monday = monday_of(datetime.fromtimestamp(now_ts, tz=engine._tz).date())
    snapshot = maturity_snapshot(engine)
    for pending in (monday - timedelta(days=7), monday):
        if weeks and weeks[-1]["week_start"] >= pending.isoformat():
            continue
        row = {
            "week_start": pending.isoformat(),
            "complete": False,
            "backfilled": False,
            **week_payload(engine, pending, now_ts, snapshot),
        }
        # The week before only while it is waiting to be closed, and only if
        # it has anything; the running week always, so the dashboard has a
        # row to call "running" on a Monday morning.
        if pending == monday or (weeks and row["days_scored"]):
            weeks.append(row)
    return {
        "version": RESPONSE_VERSION,
        "issue_hour_local": DAY_AHEAD_ISSUE_HOUR_LOCAL,
        "weeks": weeks,
    }
