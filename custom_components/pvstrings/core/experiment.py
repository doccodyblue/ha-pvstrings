"""Does reading the sensor through a curve forecast better?

Two branches run side by side: the one whose forecast is published, and one
that reads the same irradiance sensor through a calibration curve and has
learned its own models from scratch.  This module archives what each of them
said about every scored hour, and turns the archive into the one number the
decision rests on.

Three things about that number were got wrong in the first design and are
worth stating, because each of them would have produced a confident and
meaningless answer.

**Spread is not accuracy.**  An earlier criterion asked whether the hourly
profile flattened.  A branch predicting 38 % low at every hour of the day has
a perfectly flat profile and would have won.  What is measured here is the
largest distance from one, over the hours of the day -- which a uniform error
cannot improve and a structured one cannot hide in.

**Selecting clear hours by production conditions on the answer.**  Picking the
brightest quarter of each hour-of-day selects the hours the forecast was most
likely to under-predict, whatever the model.  A forecast that hits the
expected value exactly looks 67 % low in that slice.  Clear hours are
therefore chosen by the *measured* clearness index, which neither branch's
forecast has any say in.

**And by rank, not by threshold.**  The sensor's scale error is the subject of
the experiment, so a fixed cut on its clearness index would mean different
things on a plant whose sensor reads low and one whose sensor is right.  Rank
is invariant to any monotone scale error, and the same hours are selected
whichever branch is being scored.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from .quality import VALUE_MEASURED

HOUR = 3600

#: Share of each hour-of-day's archived hours counted as clear.  The top
#: third: enough to leave a usable sample on a German autumn, tight enough
#: that a genuinely overcast day cannot reach it.
CLEAR_QUANTILE = 1.0 / 3.0

#: An hour-of-day contributes to the profile only with this many clear days
#: behind it.  Below that its ratio is one day's weather, not the model's.
MIN_CLEAR_DAYS = 5

#: Energy below this is not worth a ratio: dawn and dusk hours divide small
#: numbers by small numbers and swamp the profile with noise.
MIN_HOUR_KWH = 0.05


def clearness(measured_wm2: float | None, clearsky_wm2: float | None) -> float | None:
    """The measured clearness index, or ``None`` when the sky was too dark."""
    if measured_wm2 is None or clearsky_wm2 is None or clearsky_wm2 <= 20.0:
        return None
    return float(measured_wm2) / float(clearsky_wm2)


def _local_hour(ts_utc: int, offset: Any) -> int:
    """The hour of the day a timestamp fell in, where the plant stands.

    ``offset`` is a zone, or a fixed number of seconds for callers that have
    no zone to hand.  A single offset for a whole archive is what a six-week
    autumn trial cannot use: after the clocks go back, a September noon would
    be filed as eleven o'clock and two different hours of the day would share
    a bucket.
    """
    if isinstance(offset, (int, float)):
        return ((int(ts_utc) + int(offset)) // HOUR) % 24
    return datetime.fromtimestamp(int(ts_utc), tz=offset).hour


def clear_hours(
    rows: Sequence[Mapping[str, Any]], offset_s: int = 0
) -> list[Mapping[str, Any]]:
    """The clearest third of each hour of the day, by rank.

    Ranked within the hour of day rather than over the whole archive: at 53
    degrees north a clear 08:00 in November is darker than an overcast 13:00
    in September, and a single ranking would select for season instead of for
    sky.
    """
    by_hour: dict[int, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("clearness") is None or row.get("censored"):
            continue
        by_hour.setdefault(_local_hour(row["ts_utc"], offset_s), []).append(row)

    out: list[Mapping[str, Any]] = []
    for group in by_hour.values():
        ranked = sorted(group, key=lambda r: float(r["clearness"]), reverse=True)
        keep = max(1, int(round(len(ranked) * CLEAR_QUANTILE)))
        out.extend(ranked[:keep])
    return out


def profile(
    rows: Iterable[Mapping[str, Any]], field: str, offset_s: int = 0
) -> dict[int, float]:
    """Forecast over actual, per hour of the day, as a ratio of sums.

    Ratio of sums and not a mean of ratios: a dim hour with a wild quotient
    must not outvote a bright one, which is the mistake the source-bias
    estimator was rebuilt to stop making.
    """
    totals: dict[int, list[float]] = {}
    days: dict[int, set[int]] = {}
    for row in rows:
        actual = row.get("actual_kwh")
        forecast = row.get(field)
        if actual is None or forecast is None or actual < MIN_HOUR_KWH:
            continue
        hour = _local_hour(row["ts_utc"], offset_s)
        bucket = totals.setdefault(hour, [0.0, 0.0])
        bucket[0] += float(forecast)
        bucket[1] += float(actual)
        days.setdefault(hour, set()).add(int(row["ts_utc"]) // 86400)

    return {
        hour: bucket[0] / bucket[1]
        for hour, bucket in totals.items()
        if bucket[1] > 0.0 and len(days[hour]) >= MIN_CLEAR_DAYS
    }


def worst_hour(shape: Mapping[int, float]) -> float | None:
    """How far the worst hour of the day is from getting it right.

    One number carrying both failings at once.  A branch that is uniformly
    wrong cannot improve it by flattening, and a branch whose average is
    right cannot hide a morning at 1.46 behind an afternoon at 0.70.
    """
    if not shape:
        return None
    return max(abs(ratio - 1.0) for ratio in shape.values())


#: How much of the worst hour's error has to disappear before the calibrated
#: branch is called the better one.  Half: smaller than that and a season's
#: drift could carry it.
MIN_IMPROVEMENT = 0.5

#: And how far its overall level may drift while doing so.  Without this the
#: criterion has a hole: a branch that is uniformly 38 percent low has a
#: *smaller* worst hour than one whose mornings run 46 percent high, so it
#: would win on shape alone while being wrong all day.
MAX_LEVEL_DRIFT = 0.02


def _level(rows: Iterable[Mapping[str, Any]], field: str) -> float | None:
    """Forecast over actual across every selected hour, as a ratio of sums."""
    forecast = actual = 0.0
    for row in rows:
        value, truth = row.get(field), row.get("actual_kwh")
        if value is None or truth is None or truth < MIN_HOUR_KWH:
            continue
        forecast += float(value)
        actual += float(truth)
    return forecast / actual if actual > 0.0 else None


#: Clear days the trial needs before it may conclude anything.  Fifteen, and
#: the number is set by arithmetic rather than taste: a six-to-eight week
#: trial is 42 to 56 days, of which the clearest third is 14 to 19.  Asking
#: for twenty would mean the trial could not finish at the length it was
#: planned for -- which is how a criterion quietly turns into "run it longer
#: until it passes".
MIN_TRIAL_DAYS = 15


def decides(
    result: Mapping[str, Any], min_days: int = MIN_TRIAL_DAYS
) -> dict[str, Any]:
    """Whether the trial has decided anything, and what.

    Written down before the trial begins and kept in the code rather than in
    a note, because a criterion that lives in prose gets reread in the light
    of the result.  Both conditions have to hold: the worst hour of the day
    has to lose half its error, *and* the overall level must not drift while
    it happens.
    """
    reasons: list[str] = []
    live_worst = result.get("live_worst_hour")
    shadow_worst = result.get("shadow_worst_hour")
    live_level = result.get("live_level")
    shadow_level = result.get("shadow_level")

    if result.get("days", 0) < min_days:
        reasons.append(f"only {result.get('days', 0)} clear days, needs {min_days}")
    if live_worst is None or shadow_worst is None:
        reasons.append("no profile on one of the branches")
    elif shadow_worst > live_worst * (1.0 - MIN_IMPROVEMENT):
        reasons.append(
            f"worst hour {shadow_worst:.2f} against {live_worst:.2f},"
            f" needs {live_worst * (1.0 - MIN_IMPROVEMENT):.2f}"
        )
    if live_level is None or shadow_level is None:
        reasons.append("no level on one of the branches")
    elif abs(shadow_level - 1.0) > abs(live_level - 1.0) + MAX_LEVEL_DRIFT:
        # The hole the shape criterion leaves: flat and wrong beats uneven
        # and right, unless the level is watched too.
        reasons.append(
            f"level {shadow_level:.3f} drifted further than {live_level:.3f}"
        )

    return {
        "decided": not reasons,
        "blocking": reasons,
        # Spelled out rather than left inside the sentences above: a card that
        # had to parse "needs 15" out of prose would break the first time the
        # prose changed.
        "days": result.get("days", 0),
        "days_needed": min_days,
        "min_improvement": MIN_IMPROVEMENT,
        "max_level_drift": MAX_LEVEL_DRIFT,
    }


def compare(
    rows: Sequence[Mapping[str, Any]],
    offset_s: int = 0,
    horizon: str = "da",
) -> dict[str, Any]:
    """Both branches over the same clear hours, and who is closer.

    ``horizon`` picks which pairing to judge: ``da`` is the evening-before
    issue, where the bias model and the weather class decide and the nowcast
    does not reach; ``now`` is the last issue before the hour, which includes
    the nowcast.  They answer different questions and the trial reports both.
    """
    suffix = "_da_kwh" if horizon == "da" else "_kwh"
    # One sample, built before anything is measured on it.  Taking the hours
    # of the day the two branches happen to share is not enough: a branch
    # that failed for ten of fifteen days still appears in every hour of the
    # day, and every figure -- its profile, its level, the day count -- would
    # then come from a different set of hours than the branch it is compared
    # with.  A counterexample scored 100 percent improvement on five real
    # days of evidence.
    selected = [
        row
        for row in clear_hours(rows, offset_s)
        if row.get(f"live{suffix}") is not None
        and row.get(f"shadow{suffix}") is not None
    ]
    live = profile(selected, f"live{suffix}", offset_s)
    shadow = profile(selected, f"shadow{suffix}", offset_s)
    shared = sorted(set(live) & set(shadow))
    live = {hour: live[hour] for hour in shared}
    shadow = {hour: shadow[hour] for hour in shared}

    live_worst = worst_hour(live)
    shadow_worst = worst_hour(shadow)
    days = {int(row["ts_utc"]) // 86400 for row in selected}
    return {
        "live_level": _level(selected, f"live{suffix}"),
        "shadow_level": _level(selected, f"shadow{suffix}"),
        "horizon": horizon,
        "hours_archived": len(rows),
        "hours_clear": len(selected),
        "days": len(days),
        "hours_of_day": shared,
        "live_profile": {str(h): round(live[h], 3) for h in shared},
        "shadow_profile": {str(h): round(shadow[h], 3) for h in shared},
        "live_worst_hour": None if live_worst is None else round(live_worst, 3),
        "shadow_worst_hour": None if shadow_worst is None else round(shadow_worst, 3),
        "improvement": (
            None
            if not live_worst or shadow_worst is None
            else round(1.0 - shadow_worst / live_worst, 3)
        ),
    }


def archive(engine: Any, start_ts: int, end_ts: int) -> int:
    """Put the trial's evidence somewhere the compaction cannot reach it.

    Called once an hour with the window that has just been learned.  It reads
    the same pairings the scores read -- so what is archived is what would
    have been scored -- and adds the two things the forecast log does not
    carry: how clear the sky measurably was, and whether either branch
    thought the hour was curtailed.

    Its own table because the forecast log is thinned after 35 days to one
    issue per hour: of a 56-day trial, only the last 35 evening-before issues
    would survive, and the trial would end up judged on its own second half.
    """
    store = engine.store
    lead0 = {
        (int(row["ts_utc"]), row["string_id"]): row
        for row in store.forecast_vs_actual(start_ts, end_ts, lead_time_h=0.0)
    }
    if not lead0:
        return 0

    day_ahead: dict[tuple[int, str], Any] = {}
    for day_start, day_end in _local_days(engine, start_ts, end_ts):
        cutoff = engine.day_ahead_cutoff(day_start)
        # Each cutoff over its own day, not over the whole window.  A pass
        # catching up on several days would otherwise re-query every earlier
        # day with a later cutoff and overwrite its evening-before figure
        # with one issued the same morning -- and the trial would judge
        # short-term forecasts as if they were day-ahead ones.
        for row in store.forecast_vs_actual_before(
            max(day_start, start_ts), min(day_end, end_ts), cutoff
        ):
            day_ahead[(int(row["ts_utc"]), row["string_id"])] = row

    measured = store.measured_ghi_hours(start_ts, end_ts, min_samples=1)
    clear = _clearsky_by_hour(engine, sorted(measured))

    censored = {
        (hour, string_id)
        for hour, string_id in set(engine.own_censored) | set(engine.censored_hours)
    }

    rows = []
    for key, row in lead0.items():
        hour, string_id = key
        da = day_ahead.get(key)
        rows.append(
            (
                hour,
                string_id,
                row["energy_kwh"],
                row["potential_kwh"],
                row["calibrated_kwh"],
                None if da is None else da["potential_kwh"],
                None if da is None else da["calibrated_kwh"],
                clearness(measured.get(hour), clear.get(hour)),
                1 if key in censored or row["value_kind"] != VALUE_MEASURED else 0,
            )
        )
    return store.archive_experiment_hours(rows)


def _local_days(
    engine: Any, start_ts: int, end_ts: int
) -> list[tuple[int, int]]:
    """The local days the window touches, each as its own half-open range."""
    from .forecast import local_midnight

    tz = engine._tz
    out: list[tuple[int, int]] = []
    day = local_midnight(start_ts, 0, tz)
    while day < end_ts:
        nxt = local_midnight(day, 1, tz)
        out.append((day, nxt))
        day = nxt
    return out


def _clearsky_by_hour(engine: Any, hours: Sequence[int]) -> dict[int, float]:
    if not hours:
        return {}
    from .physics import to_index

    index = to_index([hour + HOUR // 2 for hour in hours])
    values = engine.physics.clearsky(index)["ghi"].to_numpy()
    return {hour: float(value) for hour, value in zip(hours, values)}
