"""The sky map: what the string can see, and what it cannot.

The properties pinned here are the ones that decide whether the correction
helps or quietly destroys the forecast.  A map that invents values for sky it
has never seen, that lets a throttled afternoon masquerade as a shadow, or that
swallows the string's overall level, is worse than no map at all -- because the
log-ratio layer above it will then spend weeks trying to undo it.
"""

from __future__ import annotations

import pytest

from core.shading import (
    ASCENDING,
    DESCENDING,
    MIN_OBSERVATIONS,
    RECENCY_HALFLIFE_DAYS,
    ShadingMap,
    ShadingModel,
    azimuth_bin,
    elevation_bin,
    recency_weight,
    season_half,
)

DAY = 86400
#: 2025-08-15, in the descending half of the year.
SUMMER = 1_755_216_000
#: 2025-04-15, the same sun positions on the way up.
SPRING = 1_744_675_200


def observations(
    azimuth: float,
    elevation: float,
    ratio: float,
    count: int,
    weight: float = 1.0,
    ts: float = SUMMER,
) -> list[tuple[float, float, float, float, float]]:
    """``count`` observations of one cell, one per day so ages stay realistic."""
    return [
        (ts + index * DAY, azimuth, elevation, ratio, weight)
        for index in range(count)
    ]


def full_sky(ratio: float = 1.0, count: int = 40):
    """An arc of clean observations across the southern sky.

    Forty per cell is what a few weeks of five-minute collection actually
    delivers, and it matters: the shrinkage towards no-correction is meant to
    hold back thin cells, so a test seeded with ten would measure the
    shrinkage rather than the thing it claims to test.
    """
    rows = []
    for azimuth in range(90, 271, 10):
        for elevation in (15.0, 25.0, 35.0, 45.0):
            rows += observations(float(azimuth), elevation, ratio, count)
    return rows


def shade(
    rows,
    azimuth: float,
    elevation: float,
    ratio: float,
    count: int = 40,
    ts: float = SUMMER,
):
    """Replace a cell's observations -- a shadow is there on every day.

    Replace, not append: leaving the clean rows in place and adding shaded ones
    on top describes a cell that is sometimes clear, and the upper envelope
    will quite correctly report it as unshaded.
    """
    kept = [row for row in rows if not (row[1] == azimuth and row[2] == elevation)]
    return kept + observations(azimuth, elevation, ratio, count, ts=ts)


class TestBinning:
    def test_azimuth_wraps(self):
        assert azimuth_bin(0.0) == azimuth_bin(360.0)
        assert azimuth_bin(365.0) == azimuth_bin(5.0)

    def test_bins_are_contiguous(self):
        assert azimuth_bin(9.99) == 0
        assert azimuth_bin(10.0) == 1

    def test_elevation_bins_start_at_the_horizon(self):
        assert elevation_bin(0.0) == 0
        assert elevation_bin(4.9) == 0
        assert elevation_bin(5.0) == 1


class TestAnUnshadedString:
    def test_a_clean_sky_corrects_nothing(self):
        sky = ShadingMap.fit(full_sky(ratio=1.0))
        for azimuth in (100.0, 180.0, 250.0):
            assert sky.factor(azimuth, 30.0) == pytest.approx(1.0, abs=0.02)

    def test_a_uniformly_weak_string_is_not_a_shaded_one(self):
        """Level belongs to the string effect; the map carries only shape.

        A panel that under-delivers by the same fraction everywhere has a
        nameplate or a wiring problem, not a shadow.  Correcting it here would
        double up with the per-string effect the log-ratio model already
        learns, and the two would then chase each other.
        """
        sky = ShadingMap.fit(full_sky(ratio=0.70))
        for azimuth in (100.0, 180.0, 250.0):
            assert sky.factor(azimuth, 30.0) == pytest.approx(1.0, abs=0.02)


class TestAShadedCorner:
    def _sky(self):
        rows = full_sky(ratio=1.0)
        # A gable eats the low eastern sky.
        for azimuth in (90.0, 100.0, 110.0):
            rows = shade(rows, azimuth, 15.0, 0.25)
        return ShadingMap.fit(rows)

    def test_the_shaded_sector_is_corrected_down(self):
        assert self._sky().factor(100.0, 16.0) < 0.4

    def test_the_rest_of_the_sky_is_untouched(self):
        sky = self._sky()
        assert sky.factor(180.0, 30.0) == pytest.approx(1.0, abs=0.02)
        assert sky.factor(250.0, 45.0) == pytest.approx(1.0, abs=0.02)

    def test_the_edge_is_not_smeared_across_the_whole_morning(self):
        # One bin higher the string sees the sun again.
        assert self._sky().factor(100.0, 27.0) > 0.8

    def test_the_map_never_amplifies(self):
        rows = full_sky(ratio=1.0) + observations(180.0, 35.0, 1.9, 40)
        sky = ShadingMap.fit(rows)
        for azimuth in range(0, 360, 10):
            for elevation in (5.0, 25.0, 55.0):
                assert sky.factor(float(azimuth), elevation) <= 1.0


class TestCurtailmentDoesNotLookLikeShade:
    """A battery inverter throttling at noon must not become a shadow.

    Shading recurs at the same sun position on every clear day; curtailment
    only on the days the battery filled.  The mean of the two is dragged down
    and keeps sinking as more full-battery days arrive.  The upper envelope
    recovers what the string does when nothing is in the way.
    """

    def _mixed(self, throttled_fraction: float) -> ShadingMap:
        total = 40
        clean = int(total * (1.0 - throttled_fraction))
        rows = shade(full_sky(ratio=1.0), 180.0, 45.0, 1.0, count=clean)
        rows += observations(180.0, 45.0, 0.30, total - clean)
        return ShadingMap.fit(rows)

    def test_a_minority_of_throttled_days_is_ignored(self):
        assert self._mixed(0.35).factor(180.0, 46.0) > 0.9

    def test_the_mean_would_have_been_fooled(self):
        """Documents why the estimator is a quantile and not an average."""
        mean_ratio = 0.65 * 1.0 + 0.35 * 0.30
        assert mean_ratio < 0.8  # what a mean-based map would have applied
        assert self._mixed(0.35).factor(180.0, 46.0) > 0.9

    def test_a_majority_of_throttled_days_does_eventually_show(self):
        # Past the quantile the envelope has to follow -- at some point the
        # string really is only ever delivering that much.
        assert self._mixed(0.85).factor(180.0, 46.0) < 0.8


class TestThinDataIsTreatedAsThin:
    def test_a_cell_below_the_threshold_has_no_opinion(self):
        rows = full_sky(ratio=1.0)
        rows += observations(300.0, 55.0, 0.1, int(MIN_OBSERVATIONS) - 1)
        sky = ShadingMap.fit(rows)
        assert (azimuth_bin(300.0), elevation_bin(55.0)) not in sky.cells

    def test_unobserved_sky_is_never_invented(self):
        """In August the sun never reaches the winter cells.

        Extrapolating a summer factor into sky the string has not been seen in
        would be a guess presented as a measurement, and it would be applied
        every day of the winter.
        """
        sky = ShadingMap.fit(full_sky(ratio=0.4))
        assert sky.factor(180.0, 8.0) == 1.0  # below every observed cell
        assert sky.factor(20.0, 30.0) == 1.0  # due north, never observed

    def test_a_thin_cell_is_shrunk_towards_no_correction(self):
        thin = ShadingMap.fit(
            full_sky(ratio=1.0) + observations(200.0, 55.0, 0.2, 4)
        )
        thick = ShadingMap.fit(
            full_sky(ratio=1.0) + observations(200.0, 55.0, 0.2, 200)
        )
        assert thin.factor(200.0, 56.0) > thick.factor(200.0, 56.0)

    def test_coverage_weights_count_for_less(self):
        strong = ShadingMap.fit(
            full_sky(ratio=1.0) + observations(200.0, 55.0, 0.2, 40, weight=1.0)
        )
        weak = ShadingMap.fit(
            full_sky(ratio=1.0) + observations(200.0, 55.0, 0.2, 40, weight=0.25)
        )
        assert weak.factor(200.0, 56.0) > strong.factor(200.0, 56.0)


class TestNeighbours:
    def test_a_gap_borrows_from_next_door(self):
        rows = []
        for azimuth in (170.0, 190.0):
            rows += observations(azimuth, 35.0, 0.3, 40)
        rows += observations(120.0, 35.0, 1.0, 40)
        sky = ShadingMap.fit(rows)
        # 180 deg was never observed but sits between two shaded cells.
        assert sky.factor(180.0, 36.0) < 0.6

    def test_borrowing_is_one_hop_only(self):
        rows = observations(170.0, 35.0, 0.3, 40) + observations(120.0, 35.0, 1.0, 40)
        sky = ShadingMap.fit(rows)
        # Three bins away: out of reach, so no correction at all.
        assert sky.factor(200.0, 36.0) == 1.0


class TestEmptyAndDegenerate:
    def test_no_observations_means_no_correction(self):
        sky = ShadingMap.fit([])
        assert sky.factor(180.0, 30.0) == 1.0
        assert sky.observed_cells == 0

    def test_a_single_cell_cannot_shade_itself(self):
        sky = ShadingMap.fit(observations(180.0, 30.0, 0.4, 40))
        assert sky.factor(180.0, 31.0) == pytest.approx(1.0, abs=0.02)

    def test_nonsense_rows_are_dropped(self):
        sky = ShadingMap.fit(
            [
                (SUMMER, 180.0, 30.0, 0.0, 1.0),
                (SUMMER, 180.0, 30.0, -1.0, 1.0),
                (SUMMER, 180.0, -5.0, 1.0, 1.0),
            ]
        )
        assert sky.observed_cells == 0

    def test_below_the_horizon_never_corrects(self):
        sky = ShadingMap.fit(full_sky(ratio=0.3))
        assert sky.factor(180.0, -2.0) == 1.0


class TestModel:
    def test_an_unknown_string_is_uncorrected(self):
        model = ShadingModel.fit({"s1": full_sky(ratio=0.3)})
        assert model.factor("nobody", 180.0, 30.0) == 1.0

    def test_strings_do_not_share_a_sky(self):
        model = ShadingModel.fit(
            {
                "shaded": shade(full_sky(1.0), 110.0, 15.0, 0.2),
                "clear": full_sky(1.0),
            }
        )
        assert model.factor("shaded", 110.0, 16.0) < 0.5
        assert model.factor("clear", 110.0, 16.0) == pytest.approx(1.0, abs=0.05)

    def test_vectorised_lookup_matches_the_scalar_one(self):
        model = ShadingModel.fit({"s1": shade(full_sky(1.0), 110.0, 15.0, 0.2)})
        azimuths = [100.0, 110.0, 180.0, 250.0]
        elevations = [16.0, 16.0, 35.0, 45.0]
        vector = model.factors("s1", azimuths, elevations)
        for index, (azimuth, elevation) in enumerate(zip(azimuths, elevations)):
            assert vector[index] == pytest.approx(model.factor("s1", azimuth, elevation))

    def test_an_empty_model_returns_ones(self):
        assert list(ShadingModel().factors("s1", [1.0, 2.0], [3.0, 4.0])) == [1.0, 1.0]

    def test_summary_lists_the_worst_sectors_first(self):
        """Reported as a loss, worst first, matching what the name promises."""
        model = ShadingModel.fit({"s1": shade(full_sky(1.0), 110.0, 15.0, 0.2)})
        sectors = model.summary()["s1"]["most_shaded"]
        worst = sectors[0]
        assert worst["shading_pct"] > 50.0
        assert worst["sector"].startswith("110-120")
        assert [s["shading_pct"] for s in sectors] == sorted(
            (s["shading_pct"] for s in sectors), reverse=True
        )

    def test_an_unshaded_sector_reports_no_loss(self):
        model = ShadingModel.fit({"s1": full_sky(1.0)})
        for sector in model.summary().get("s1", {}).get("most_shaded", []):
            assert sector["shading_pct"] == pytest.approx(0.0, abs=2.0)


class TestSeasons:
    """A wall shades the same all year; a tree does not.

    The sun reaches any given point in the sky twice a year, once on the way up
    and once on the way down.  Those two visits are indistinguishable to a map
    indexed on sun position -- which is fine for masonry and quite wrong for a
    cherry tree, bare in April and in full leaf in September.
    """

    CELL = (150.0, 55.0)

    def _tree(self, spring_ratio: float, autumn_ratio: float) -> ShadingMap:
        rows = shade(full_sky(1.0), *self.CELL, spring_ratio, count=30, ts=SPRING)
        rows += observations(*self.CELL, autumn_ratio, 30, ts=SUMMER)
        return ShadingMap.fit(rows, now_ts=SUMMER + 60 * DAY)

    def test_the_two_halves_of_the_year_are_told_apart(self):
        sky = self._tree(spring_ratio=0.90, autumn_ratio=0.45)
        spring = sky.factor(*self.CELL, ts_utc=SPRING)
        autumn = sky.factor(*self.CELL, ts_utc=SUMMER)
        assert spring > autumn * 1.5

    def test_a_pooled_answer_lies_between_them(self):
        """Without a timestamp the caller gets the year-round average."""
        sky = self._tree(spring_ratio=0.90, autumn_ratio=0.45)
        pooled = sky.factor(*self.CELL)
        assert sky.factor(*self.CELL, ts_utc=SUMMER) <= pooled

    def test_masonry_is_not_split(self):
        """Equal halves must keep one cell and all of its evidence.

        Splitting halves the observations behind each side, so a site shaded
        by buildings would pay for a distinction it does not need.
        """
        sky = self._tree(spring_ratio=0.50, autumn_ratio=0.50)
        assert sky.seasonal == {}
        assert sky.factor(*self.CELL, ts_utc=SPRING) == sky.factor(
            *self.CELL, ts_utc=SUMMER
        )

    def test_a_small_difference_is_not_worth_a_split(self):
        sky = self._tree(spring_ratio=0.55, autumn_ratio=0.50)
        assert sky.seasonal == {}

    def test_a_split_needs_days_not_just_readings(self):
        """Eight readings can be one afternoon; a season needs many days.

        Backfilled hours and live five-minute intervals arrive at wildly
        different rates, so the bar has to be something they share.
        """
        rows = shade(full_sky(1.0), *self.CELL, 0.9, count=30, ts=SPRING)
        # Thirty autumn readings, but all crammed into three days.
        rows += [
            (SUMMER + (index % 3) * DAY, *self.CELL, 0.45, 1.0)
            for index in range(30)
        ]
        assert ShadingMap.fit(rows, now_ts=SUMMER + 60 * DAY).seasonal == {}

    def test_one_half_alone_never_splits(self):
        """A plant installed in June has no spring to compare against yet."""
        rows = shade(full_sky(1.0), *self.CELL, 0.4)
        assert ShadingMap.fit(rows).seasonal == {}

    def test_the_solstices_bound_the_halves(self):
        # 1 March rising, 1 October falling.
        assert season_half(1_740_787_200) == ASCENDING
        assert season_half(1_759_276_800) == DESCENDING

    def test_the_split_is_hemisphere_independent(self):
        """Declination rises from December to June everywhere on Earth."""
        assert season_half(SPRING) == ASCENDING
        assert season_half(SUMMER) == DESCENDING


class TestForgetting:
    def test_recent_evidence_outweighs_old(self):
        now = SUMMER + 4 * 365 * DAY
        assert recency_weight(now, now) == 1.0
        assert recency_weight(now - RECENCY_HALFLIFE_DAYS * DAY, now) == pytest.approx(
            0.5
        )

    def test_a_felled_tree_stops_being_believed(self):
        """Four years of shade, then a clear season, and the map lets go."""
        rows = shade(
            full_sky(1.0), 150.0, 35.0, 0.3, count=200, ts=SUMMER - 4 * 365 * DAY
        )
        rows += observations(150.0, 35.0, 1.0, 60, ts=SUMMER)
        sky = ShadingMap.fit(rows, now_ts=SUMMER + 30 * DAY)
        assert sky.factor(150.0, 36.0) > 0.8

    def test_without_recent_evidence_the_old_still_counts(self):
        """A winter cell nobody has visited since last year is all we have."""
        rows = shade(
            full_sky(1.0), 150.0, 15.0, 0.3, count=60, ts=SUMMER - 300 * DAY
        )
        sky = ShadingMap.fit(rows, now_ts=SUMMER + 30 * DAY)
        assert sky.factor(150.0, 16.0) < 0.6

    def test_age_is_measured_against_the_data_not_the_clock(self):
        """Refitting an old database must not erase it."""
        rows = shade(full_sky(1.0), 150.0, 35.0, 0.3, count=60)
        assert ShadingMap.fit(rows).factor(150.0, 36.0) < 0.6


class TestTheReferenceComesFromEvidence:
    """Thin cells must not define what "unshaded" means.

    Shrinkage drags a sparse cell towards no-correction, so a reference taken
    over shrunk values lets the emptiest corners of the sky set the standard.
    A clear string whose physics runs a little optimistic then comes out shaded
    everywhere -- swallowing exactly the level the per-string effect exists to
    carry, and leaving the two layers to fight over it.
    """

    def test_an_optimistic_but_unshaded_string_is_not_corrected(self):
        """Physics 15 % optimistic across a sky with uneven coverage."""
        rows = []
        for azimuth in range(90, 271, 10):
            for elevation, count in ((15.0, 5), (25.0, 60), (35.0, 60), (45.0, 8)):
                rows += observations(float(azimuth), elevation, 0.85, count)
        sky = ShadingMap.fit(rows)
        for azimuth in (100.0, 180.0, 250.0):
            assert sky.factor(azimuth, 26.0) == pytest.approx(1.0, abs=0.03)

    def test_a_genuine_shadow_still_shows_through(self):
        rows = []
        for azimuth in range(90, 271, 10):
            for elevation, count in ((15.0, 5), (25.0, 60), (35.0, 60), (45.0, 8)):
                rows += observations(float(azimuth), elevation, 0.85, count)
        rows = shade(rows, 110.0, 25.0, 0.30, count=60)
        sky = ShadingMap.fit(rows)
        assert sky.factor(110.0, 26.0) < 0.45


class TestRefitIsThrottled:
    """Refitting every daylight hour is a visible load on a small host.

    In steady state the table holds one row per five-minute interval per
    string for the whole retention window; pulling all of them out of SQLite
    once an hour buys nothing, because a sky map does not change materially
    between one hour and the next.
    """

    def test_the_same_day_does_not_refit(self, seeded_store, plant):
        from core.forecast import ForecastEngine

        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        calls = {"n": 0}
        original = seeded_store.shading_rows_by_string

        def counting():
            calls["n"] += 1
            return original()

        seeded_store.shading_rows_by_string = counting
        engine.fit_shading(SUMMER)
        engine.fit_shading(SUMMER + 3600)
        engine.fit_shading(SUMMER + 7200)
        assert calls["n"] == 1

    def test_a_new_day_refits(self, seeded_store, plant):
        from core.forecast import ForecastEngine

        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        calls = {"n": 0}
        original = seeded_store.shading_rows_by_string

        def counting():
            calls["n"] += 1
            return original()

        seeded_store.shading_rows_by_string = counting
        engine.fit_shading(SUMMER)
        engine.fit_shading(SUMMER + 86400)
        assert calls["n"] == 2

    def test_force_always_refits(self, seeded_store, plant):
        from core.forecast import ForecastEngine

        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        calls = {"n": 0}
        original = seeded_store.shading_rows_by_string

        def counting():
            calls["n"] += 1
            return original()

        seeded_store.shading_rows_by_string = counting
        engine.fit_shading(SUMMER)
        engine.fit_shading(SUMMER, force=True)
        assert calls["n"] == 2


class TestTheFactorReachesThePhysics:
    """Wiring, not arithmetic -- and the wiring is where this went wrong.

    `_interval_power` computed a solar position for the shading lookup and then
    called `physics.run` without passing the factor, so the two supposedly
    separate passes in the learn cycle were byte-identical unshaded physics.
    Nothing failed: the map was still built, still summarised, still shown --
    it just never touched a number. Meanwhile the log-ratio model absorbed the
    shadow into its per-string effect and the forecast path subtracted it a
    second time.
    """

    def _engine(self, seeded_store, plant):
        from core.forecast import ForecastEngine

        engine = ForecastEngine(plant, seeded_store)
        engine.load_models()
        return engine

    def _uniform_map(self, string_id: str, factor: float) -> ShadingModel:
        """A map built by hand, not fitted.

        Fitting a uniformly darkened sky quite correctly yields no correction
        at all -- an even loss across every sun position is level, and level
        belongs to the per-string effect.  This test is about whether the
        factor reaches `physics.run`, so the map is constructed directly and
        the fitter is left out of it.
        """
        import math

        from core.shading import Cell, ShadingMap

        cells = {
            (azimuth, elevation): Cell(value=math.log(factor), n=10_000.0)
            for azimuth in range(36)
            for elevation in range(19)
        }
        return ShadingModel(maps={string_id: ShadingMap(cells=cells, reference=0.0)})

    def _conditions(self, engine, start, end):
        index = engine._midpoint_index(start, end)
        return index, engine._actual_conditions(index, start, end)

    def test_shaded_and_unshaded_passes_differ(self, seeded_store, plant):
        import test_forecast_engine as fe

        engine = self._engine(seeded_store, plant)
        start = fe.DAY_START + 11 * fe.HOUR
        end = start + fe.HOUR
        fe.clear_sky_forecast(engine, seeded_store, start - fe.HOUR, start, hours=2)
        index, conditions = self._conditions(engine, start, end)
        assert conditions is not None

        raw = engine._interval_power(index, conditions, apply_shading=False)
        engine.shading = self._uniform_map("s1", 0.5)
        shaded = engine._interval_power(index, conditions, apply_shading=True)

        raw_total = sum(raw["s1"].values())
        shaded_total = sum(shaded["s1"].values())
        assert raw_total > 0
        assert shaded_total == pytest.approx(raw_total * 0.5, rel=0.05)

    def test_only_the_named_string_is_affected(self, seeded_store, plant):
        import test_forecast_engine as fe

        engine = self._engine(seeded_store, plant)
        start = fe.DAY_START + 11 * fe.HOUR
        end = start + fe.HOUR
        fe.clear_sky_forecast(engine, seeded_store, start - fe.HOUR, start, hours=2)
        index, conditions = self._conditions(engine, start, end)
        raw = engine._interval_power(index, conditions, apply_shading=False)
        engine.shading = self._uniform_map("s1", 0.5)
        shaded = engine._interval_power(index, conditions, apply_shading=True)
        assert sum(shaded["s2"].values()) == pytest.approx(sum(raw["s2"].values()))

    def test_an_empty_map_changes_nothing(self, seeded_store, plant):
        import test_forecast_engine as fe

        engine = self._engine(seeded_store, plant)
        start = fe.DAY_START + 11 * fe.HOUR
        end = start + fe.HOUR
        fe.clear_sky_forecast(engine, seeded_store, start - fe.HOUR, start, hours=2)
        index, conditions = self._conditions(engine, start, end)
        raw = engine._interval_power(index, conditions, apply_shading=False)
        shaded = engine._interval_power(index, conditions, apply_shading=True)
        assert sum(shaded["s1"].values()) == pytest.approx(sum(raw["s1"].values()))


class TestTheUserSwitchIsHonoured:
    """"Apply learned correction" has to mean all of them.

    The log-ratio layer checked `plant.learning_enabled`; the shading map only
    checked the internal `apply_learning` argument, which the normal forecast
    always passes as true.  A plant with the switch off therefore kept being
    multiplied down by a map it had been told to ignore -- and, with learning
    off, no longer collected the observations that would have justified it.
    """

    def _forecast_total(self, seeded_store, plant, learning_enabled: bool) -> float:
        from dataclasses import replace

        from core.forecast import ForecastEngine
        import test_forecast_engine as fe

        engine = ForecastEngine(replace(plant, learning_enabled=learning_enabled), seeded_store)
        engine.load_models()
        start = fe.DAY_START + 11 * fe.HOUR
        fe.clear_sky_forecast(engine, seeded_store, start - fe.HOUR, start, hours=2)

        import math

        from core.shading import Cell, ShadingMap

        engine.shading = ShadingModel(
            maps={
                "s1": ShadingMap(
                    cells={
                        (azimuth, elevation): Cell(value=math.log(0.5), n=10_000.0)
                        for azimuth in range(36)
                        for elevation in range(19)
                    },
                    reference=0.0,
                )
            }
        )
        rows = engine.forecast(start, hours=1, start_ts=start)
        return sum(row.potential_kwh for row in rows if row.string_id == "s1")

    def test_the_map_applies_when_learning_is_on(self, seeded_store, plant):
        on = self._forecast_total(seeded_store, plant, learning_enabled=True)
        off = self._forecast_total(seeded_store, plant, learning_enabled=False)
        assert on > 0 and off > 0
        assert on == pytest.approx(off * 0.5, rel=0.05)

    def test_the_map_is_silent_when_learning_is_off(self, seeded_store, plant):
        off = self._forecast_total(seeded_store, plant, learning_enabled=False)
        # Same plant, no map at all -- must be indistinguishable.
        from dataclasses import replace

        from core.forecast import ForecastEngine
        import test_forecast_engine as fe

        engine = ForecastEngine(replace(plant, learning_enabled=False), seeded_store)
        engine.load_models()
        start = fe.DAY_START + 11 * fe.HOUR
        rows = engine.forecast(start, hours=1, start_ts=start)
        bare = sum(row.potential_kwh for row in rows if row.string_id == "s1")
        assert off == pytest.approx(bare, rel=1e-6)
