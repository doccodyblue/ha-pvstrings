"""Outlook sensors for days +3..+5: whole days or nothing."""

from __future__ import annotations

from core.aggregate import complete_day_totals

DAY = 86400
D0 = 1_800_000_000 - 1_800_000_000 % DAY


def _hours(start, n, value=1.0):
    return [(start + 3600 * i, value) for i in range(n)]


def test_whole_days_are_summed():
    rows = _hours(D0, 48, 0.5)
    assert complete_day_totals(rows, [(D0, D0 + DAY), (D0 + DAY, D0 + 2 * DAY)]) == [12.0, 12.0]


def test_a_day_with_a_missing_hour_is_none_not_small():
    rows = _hours(D0, 24)
    del rows[13]
    assert complete_day_totals(rows, [(D0, D0 + DAY)]) == [None]


def test_range_ending_inside_a_day_leaves_that_day_empty():
    rows = _hours(D0, 30)
    assert complete_day_totals(rows, [(D0, D0 + DAY), (D0 + DAY, D0 + 2 * DAY)]) == [24.0, None]


def test_strings_are_added_per_hour():
    rows = _hours(D0, 24, 1.0) + _hours(D0, 24, 0.5)
    assert complete_day_totals(rows, [(D0, D0 + DAY)]) == [36.0]


def test_dst_days_use_their_own_length():
    rows = _hours(D0, 23 + 25)
    bounds = [(D0, D0 + 23 * 3600), (D0 + 23 * 3600, D0 + 48 * 3600)]
    assert complete_day_totals(rows, bounds) == [23.0, 25.0]


def test_negative_hours_do_not_reduce_a_day():
    rows = _hours(D0, 24)
    rows[0] = (D0, -5.0)
    assert complete_day_totals(rows, [(D0, D0 + DAY)]) == [23.0]


def test_outlook_constants():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "custom_components/pvstrings/const.py").read_text()
    assert "OUTLOOK_DAYS: Final = (3, 4, 5)" in src
    assert "WEATHER_FORECAST_DAYS: Final = OUTLOOK_DAYS[-1] + 2" in src


# --------------------------------------------------------------------------- #
# engine level: the longer weather fetch must not move the live 72 h run
# --------------------------------------------------------------------------- #

from core.forecast import HOUR  # noqa: E402
from test_forecast_engine import DAY_START, clear_sky_forecast  # noqa: E402
from test_minimal_plant import minimal_engine, minimal_plant  # noqa: E402,F401


def test_rows_beyond_the_live_window_do_not_change_it(minimal_engine, store):
    clear_sky_forecast(minimal_engine, store, DAY_START, DAY_START, 72)
    before = minimal_engine.forecast(DAY_START, hours=72, start_ts=DAY_START)
    # Day 3..5 rows, partly without components, as a short-range model leaves them.
    clear_sky_forecast(minimal_engine, store, DAY_START, DAY_START + 72 * HOUR, 72)
    store.upsert_weather_forecast(
        [(DAY_START, DAY_START + 140 * HOUR, "open_meteo", 140, *[None] * 11)]
    )
    after = minimal_engine.forecast(DAY_START, hours=72, start_ts=DAY_START)
    assert [(r.ts_utc, r.string_id, r.potential_kwh) for r in after] == [
        (r.ts_utc, r.string_id, r.potential_kwh) for r in before
    ]


def test_outlook_run_yields_three_whole_days(minimal_engine, store):
    clear_sky_forecast(minimal_engine, store, DAY_START, DAY_START, 144)
    start = DAY_START + 72 * HOUR
    rows = minimal_engine.forecast(DAY_START, hours=72, start_ts=start)
    bounds = [(start + d * 24 * HOUR, start + (d + 1) * 24 * HOUR) for d in range(3)]
    totals = complete_day_totals(((r.ts_utc, r.potential_kwh) for r in rows), bounds)
    assert all(t is not None and t > 0 for t in totals)
