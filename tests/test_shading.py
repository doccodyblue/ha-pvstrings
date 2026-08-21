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

    def test_a_shadow_the_sun_visits_most_is_still_a_shadow(self):
        """The reference must never be allowed to settle inside the shadow.

        Taken from real data: a roof dark until one in the afternoon. The sun
        crosses the shaded morning sky slowly and low, so those cells collect
        the *most* observations; the clear afternoon cells are crossed fast and
        collect fewer. Filtering the reference by observation count therefore
        threw away every bright cell and kept only shaded ones -- the string
        was measured against its own shadow, every cell came out at or above
        that, clamped, and the map reported a flawless sky over a dark roof.
        """
        rows = []
        for azimuth in range(80, 140, 10):          # Vormittag: verschattet, gut belegt
            rows += observations(float(azimuth), 25.0, 0.18, 30)
        for azimuth in range(190, 260, 10):         # Nachmittag: frei, duenn belegt
            rows += observations(float(azimuth), 40.0, 1.05, 8)

        sky = ShadingMap.fit(rows)
        morgens = sky.factor(100.0, 26.0)
        nachmittags = sky.factor(210.0, 41.0)

        assert morgens < 0.45, f"Morgenschatten nicht erkannt: Faktor {morgens}"
        assert nachmittags > 0.9, f"freier Nachmittag faelschlich korrigiert: {nachmittags}"

    def test_a_string_that_beats_physics_is_not_shaded_everywhere(self):
        """Physics running low is a level, and the level is not the map's job.

        Taken from a live plant: the reference reached 1.78, so a cell matching
        physics exactly came out 44 % "shaded" and a nearly flat panel with a
        clear view of the whole sky carried 10 to 36 % loss. Ratios above one
        do not mean an absence of shadow, they mean the physics runs low there
        -- and the per-string log-ratio layer exists to carry exactly that.
        """
        rows = []
        for azimuth in range(90, 271, 10):
            for elevation in (15.0, 25.0, 35.0, 45.0):
                rows += observations(float(azimuth), elevation, 1.35, 40)

        sky = ShadingMap.fit(rows)
        for azimuth in (100.0, 180.0, 250.0):
            assert sky.factor(azimuth, 26.0) == pytest.approx(1.0, abs=1e-6)

    def test_a_shadow_is_still_measured_against_parity_not_the_best_cell(self):
        """The shadow survives the cap; it is measured against 1.0, not 1.35."""
        rows = []
        for azimuth in range(150, 271, 10):
            rows += observations(float(azimuth), 35.0, 1.35, 40)
        for azimuth in range(90, 150, 10):
            rows += observations(float(azimuth), 25.0, 0.30, 40)

        sky = ShadingMap.fit(rows)
        schatten = sky.factor(100.0, 26.0)
        # Gegen Parität landet die Zelle bei ~0.35 -- ihr Rohwert 0.30, von der
        # Schrumpfung bei n=40 leicht angehoben.  Ohne den Deckel läge die
        # Referenz bei 1.35 und dieselbe Zelle bei ~0.26: derselbe Schatten,
        # um ein Drittel übertrieben, weil der Physik-Versatz mitgerechnet wird.
        assert 0.32 < schatten < 0.40, schatten
        assert sky.factor(200.0, 36.0) == pytest.approx(1.0, abs=1e-6)

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


class TestRefitFollowsTheEvidence:
    """Refit cost scales with the table, so the trigger must scale with it too.

    Every daylight hour is far too often once the table holds a year of
    five-minute rows. Once a day is far too seldom for a map two days old,
    which gains half its size in a morning -- holding that back until tomorrow
    means a whole day of sun corrects nothing, which is exactly what happened
    on the reference plant.
    """

    @staticmethod
    def _observe(store, count, offset=0):
        store.add_shading_obs(
            [
                (SUMMER + (offset + i) * 300, "s1", 180.0, 30.0, 0.9, 1.0)
                for i in range(count)
            ]
        )

    def test_a_young_map_follows_a_morning_of_data(self, seeded_store, plant):
        from core.forecast import ForecastEngine

        engine = ForecastEngine(plant, seeded_store)
        self._observe(seeded_store, 100)
        engine.fit_shading(SUMMER, force=True)
        calls = {"n": 0}
        original = seeded_store.shading_rows_by_string

        def counting():
            calls["n"] += 1
            return original()

        seeded_store.shading_rows_by_string = counting
        # A morning adds well over ten percent to a hundred rows.
        self._observe(seeded_store, 60, offset=1000)
        engine.fit_shading(SUMMER + 3600)
        assert calls["n"] == 1, "a young map must not wait until tomorrow"

    def test_a_mature_map_ignores_the_same_morning(self, seeded_store, plant):
        from core.forecast import ForecastEngine

        engine = ForecastEngine(plant, seeded_store)
        self._observe(seeded_store, 3000)
        engine.fit_shading(SUMMER, force=True)
        calls = {"n": 0}
        original = seeded_store.shading_rows_by_string

        def counting():
            calls["n"] += 1
            return original()

        seeded_store.shading_rows_by_string = counting
        self._observe(seeded_store, 60, offset=100000)
        engine.fit_shading(SUMMER + 3600)
        assert calls["n"] == 0, "sixty rows must not move a map of three thousand"

    def test_a_handful_of_rows_does_not_thrash(self, seeded_store, plant):
        """Ten percent of almost nothing is still almost nothing."""
        from core.forecast import ForecastEngine

        engine = ForecastEngine(plant, seeded_store)
        self._observe(seeded_store, 10)
        engine.fit_shading(SUMMER, force=True)
        calls = {"n": 0}
        original = seeded_store.shading_rows_by_string
        seeded_store.shading_rows_by_string = lambda: (
            calls.__setitem__("n", calls["n"] + 1) or original()
        )
        self._observe(seeded_store, 5, offset=500)
        engine.fit_shading(SUMMER + 3600)
        assert calls["n"] == 0

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

    def _uniform_map(
        self, string_id: str, factor: float, differential: bool = False
    ) -> ShadingModel:
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
        return ShadingModel(
            maps={
                string_id: ShadingMap(
                    cells=cells, reference=0.0, differential=differential
                )
            }
        )

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

        raw, _rb = engine._interval_power(index, conditions, apply_shading=False)
        engine.shading = self._uniform_map("s1", 0.5)
        shaded, _sb = engine._interval_power(index, conditions, apply_shading=True)

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
        raw, _rb = engine._interval_power(index, conditions, apply_shading=False)
        engine.shading = self._uniform_map("s1", 0.5)
        shaded, _sb = engine._interval_power(index, conditions, apply_shading=True)
        assert sum(shaded["s2"].values()) == pytest.approx(sum(raw["s2"].values()))

    def test_an_empty_map_changes_nothing(self, seeded_store, plant):
        import test_forecast_engine as fe

        engine = self._engine(seeded_store, plant)
        start = fe.DAY_START + 11 * fe.HOUR
        end = start + fe.HOUR
        fe.clear_sky_forecast(engine, seeded_store, start - fe.HOUR, start, hours=2)
        index, conditions = self._conditions(engine, start, end)
        raw, _rb = engine._interval_power(index, conditions, apply_shading=False)
        shaded, _sb = engine._interval_power(index, conditions, apply_shading=True)
        assert sum(shaded["s1"].values()) == pytest.approx(sum(raw["s1"].values()))

    def test_a_differential_map_spares_the_diffuse(self, seeded_store, plant):
        """Same factor, beam scope: the diffuse floor survives the shadow."""
        import test_forecast_engine as fe

        engine = self._engine(seeded_store, plant)
        start = fe.DAY_START + 11 * fe.HOUR
        end = start + fe.HOUR
        fe.clear_sky_forecast(engine, seeded_store, start - fe.HOUR, start, hours=2)
        index, conditions = self._conditions(engine, start, end)
        raw, _rb = engine._interval_power(index, conditions, apply_shading=False)
        engine.shading = self._uniform_map("s1", 0.5, differential=True)
        shaded, _sb = engine._interval_power(index, conditions, apply_shading=True)
        raw_total = sum(raw["s1"].values())
        shaded_total = sum(shaded["s1"].values())
        assert raw_total * 0.5 < shaded_total < raw_total * 0.98


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

    def test_one_busy_string_does_not_hold_back_a_new_one(
        self, seeded_store, plant
    ):
        """The maps are per string, so the trigger has to be too.

        A string added later, or one whose sensor was only just repaired, has
        almost no history. Measured against a sibling's three thousand rows its
        first morning is not ten percent of anything, and it would sit
        uncorrected until the next day.
        """
        from core.forecast import ForecastEngine

        engine = ForecastEngine(plant, seeded_store)
        seeded_store.add_shading_obs(
            [(SUMMER + i * 300, "s1", 180.0, 30.0, 0.9, 1.0) for i in range(3000)]
        )
        seeded_store.add_shading_obs(
            [(SUMMER + i * 300, "s2", 180.0, 30.0, 0.9, 1.0) for i in range(40)]
        )
        engine.fit_shading(SUMMER, force=True)
        calls = {"n": 0}
        original = seeded_store.shading_rows_by_string
        seeded_store.shading_rows_by_string = lambda: (
            calls.__setitem__("n", calls["n"] + 1) or original()
        )
        # A morning for the young string; nothing at all for the busy one.
        seeded_store.add_shading_obs(
            [
                (SUMMER + (100_000 + i) * 300, "s2", 180.0, 30.0, 0.9, 1.0)
                for i in range(60)
            ]
        )
        engine.fit_shading(SUMMER + 3600)
        assert calls["n"] == 1


class TestTheGridShowsTheShape:
    """A ranked top-six hides the one thing a map is for.

    You cannot see a gable edge or the outline of a tree in a list of the six
    worst sectors -- and the shape is the whole reason for indexing on sun
    position in the first place.
    """

    def test_every_observed_cell_is_there(self):
        sky = ShadingMap.fit(full_sky(1.0))
        assert len(sky.grid()) == sky.observed_cells

    def test_cells_carry_their_place_in_the_sky(self):
        sky = ShadingMap.fit(shade(full_sky(1.0), 110.0, 15.0, 0.2))
        cell = next(c for c in sky.grid() if c["az"] == 110.0 and c["el"] == 15.0)
        assert cell["loss"] > 50.0
        assert cell["n"] > 0

    def test_an_unshaded_sky_is_all_zeros(self):
        for cell in ShadingMap.fit(full_sky(1.0)).grid():
            assert cell["loss"] == pytest.approx(0.0, abs=2.0)

    def test_an_empty_map_has_no_grid(self):
        assert ShadingMap.fit([]).grid() == []

    def test_the_model_hands_out_grids_per_string(self):
        model = ShadingModel.fit({"s1": shade(full_sky(1.0), 110.0, 15.0, 0.2)})
        assert model.grid("s1")
        assert model.grid("nobody") == []

    def test_seasonal_cells_are_exported_too(self):
        """The forecast looks them up, so a map without them lies.

        And it lies on exactly the cells where the two halves disagree, which
        is the only reason those cells exist at all.
        """
        rows = shade(full_sky(1.0), 150.0, 55.0, 0.9, count=30, ts=SPRING)
        rows += observations(150.0, 55.0, 0.45, 30, ts=SUMMER)
        sky = ShadingMap.fit(rows, now_ts=SUMMER + 60 * DAY)
        assert sky.seasonal, "the fixture must produce a split"
        cell = [c for c in sky.grid() if c["az"] == 150.0 and c["el"] == 55.0]
        seasons = {c["season"] for c in cell}
        assert seasons == {None, "ascending", "descending"}
        spring = next(c for c in cell if c["season"] == "ascending")
        autumn = next(c for c in cell if c["season"] == "descending")
        assert autumn["loss"] > spring["loss"] + 10

    def test_an_unsplit_map_reports_only_pooled_cells(self):
        sky = ShadingMap.fit(full_sky(1.0))
        assert {c["season"] for c in sky.grid()} == {None}


def joint_sky(
    specs: dict,
    days: int = 30,
    beam=1.0,
    moment=None,
    epoch0: float = SUMMER,
) -> dict[str, list[tuple]]:
    """Physically consistent rows for several strings watching one sun.

    One sweep across the southern sky per day; at every epoch all strings
    observe at once, which is what the joint fit's moment term feeds on.
    ``specs`` maps a string id to its ``level`` and an optional ``shade`` of
    ``{(azimuth, elevation): clear-day transmission}``.  The shadow costs beam
    only: at beam share ``b`` the measured ratio is
    ``level * moment * (1 - b * (1 - transmission))``, which is also the
    model the forecast applies -- the fixture and the fit share one physics.
    """
    positions = [
        (float(azimuth), elevation)
        for azimuth in range(90, 271, 10)
        for elevation in (15.0, 25.0, 35.0, 45.0)
    ]
    rows: dict[str, list[tuple]] = {string_id: [] for string_id in specs}
    for day in range(days):
        for offset, (azimuth, elevation) in enumerate(positions):
            ts = epoch0 + day * DAY + offset * 300
            common = moment(ts) if moment else 1.0
            clearness = beam(ts) if callable(beam) else beam
            for string_id, spec in specs.items():
                transmission = spec.get("shade", {}).get((azimuth, elevation), 1.0)
                biting = 1.0 - clearness * (1.0 - transmission)
                rows[string_id].append(
                    (
                        ts,
                        azimuth,
                        elevation,
                        spec["level"] * common * biting,
                        1.0,
                        100.0 * spec["level"],
                        clearness,
                    )
                )
    return rows


class TestDifferential:
    """Shade as a difference between siblings.

    These pin the property the absolute fit could not have: a shadow on a
    string whose physics runs low everywhere.  Where a shared shadow splits
    across siblings, three fixed rounds leave a geometric remnant -- the
    assertions below expect the improvement, not perfection.
    """

    def test_a_shadow_survives_a_level_above_parity(self):
        """The S2 case: every cell beats physics, the morning is still dark."""
        rows = joint_sky(
            {
                "victron": {"level": 1.5, "shade": {(110.0, 35.0): 0.5}},
                "garden": {"level": 1.3},
            }
        )
        model = ShadingModel.fit(rows)
        assert model.method == "differential"
        assert model.factor("victron", 110.0, 36.0) < 0.65
        assert model.factor("victron", 200.0, 36.0) == pytest.approx(1.0, abs=0.05)
        assert model.factor("garden", 110.0, 36.0) == pytest.approx(1.0, abs=0.05)
        # The absolute fit sees the same rows and understates the same shadow:
        # 1.5 x 0.5 = 0.75 against a reference capped at parity reads as a
        # quarter lost, not half.
        assert ShadingMap.fit(rows["victron"]).factor(110.0, 36.0) > 0.7

    def test_cloud_moments_cancel_between_siblings(self):
        moment = lambda ts: (0.7, 1.0, 1.3)[int(ts // 300) % 3]  # noqa: E731
        model = ShadingModel.fit(
            joint_sky(
                {
                    "a": {"level": 1.0, "shade": {(110.0, 35.0): 0.5}},
                    "b": {"level": 1.0},
                },
                moment=moment,
            )
        )
        assert model.factor("a", 110.0, 36.0) < 0.68
        assert model.factor("a", 200.0, 36.0) == pytest.approx(1.0, abs=0.05)
        for azimuth in (90.0, 150.0, 200.0, 260.0):
            assert model.factor("b", azimuth, 36.0) == pytest.approx(1.0, abs=0.05)

    def test_overcast_days_cannot_vote_the_shadow_away(self):
        """Half the days are grey and the obstacle takes nothing on them.

        The envelope read exactly those days as proof of clear view; the
        clearness weight makes them bystanders instead of voters.
        """
        clearness = lambda ts: 1.0 if int(ts // DAY) % 2 == 0 else 0.05  # noqa: E731
        rows = joint_sky(
            {
                "a": {"level": 1.0, "shade": {(110.0, 35.0): 0.4}},
                "b": {"level": 1.0},
            },
            days=40,
            beam=clearness,
        )
        model = ShadingModel.fit(rows)
        assert model.factor("a", 110.0, 36.0) < 0.6
        assert ShadingMap.fit(rows["a"]).factor(110.0, 36.0) > 0.9

    def test_two_strings_sharing_one_shadow_both_keep_it(self):
        model = ShadingModel.fit(
            joint_sky(
                {
                    "a": {"level": 1.4, "shade": {(110.0, 35.0): 0.4}},
                    "b": {"level": 1.2, "shade": {(110.0, 35.0): 0.6}},
                    "c": {"level": 1.0},
                }
            )
        )
        assert model.factor("a", 110.0, 36.0) < 0.65
        assert model.factor("b", 110.0, 36.0) < 0.95
        assert model.factor("a", 110.0, 36.0) < model.factor("b", 110.0, 36.0)
        assert model.factor("c", 110.0, 36.0) == pytest.approx(1.0, abs=0.05)

    def test_a_single_string_keeps_the_absolute_fit(self):
        model = ShadingModel.fit({"only": full_sky(ratio=0.7)})
        assert model.method == "absolute"
        assert model.level("only") is None
        assert model.factor("only", 180.0, 30.0) == 1.0

    def test_the_level_is_reported_not_applied(self):
        model = ShadingModel.fit(
            joint_sky({"hot": {"level": 1.5}, "cool": {"level": 0.9}})
        )
        assert model.level("hot") == pytest.approx(1.5, rel=0.1)
        assert model.level("cool") == pytest.approx(0.9, rel=0.1)
        assert model.factor("hot", 180.0, 35.0) == pytest.approx(1.0, abs=0.05)
        assert model.factor("cool", 180.0, 35.0) == pytest.approx(1.0, abs=0.05)

    def test_clearness_scales_the_applied_correction(self):
        model = ShadingModel.fit(
            joint_sky(
                {
                    "a": {"level": 1.0, "shade": {(110.0, 35.0): 0.5}},
                    "b": {"level": 1.0},
                }
            )
        )
        geometric = model.factor("a", 110.0, 36.0)
        assert geometric < 0.65
        assert model.factor("a", 110.0, 36.0, beam=0.0) == pytest.approx(1.0)
        assert model.factor("a", 110.0, 36.0, beam=1.0) == pytest.approx(geometric)
        assert model.factor("a", 110.0, 36.0, beam=0.5) == pytest.approx(
            1.0 - 0.5 * (1.0 - geometric)
        )
        # Unknown clearness applies in full -- ignorance degrades to the old
        # behaviour, never to optimism.
        vector = model.factors("a", [110.0], [36.0], beam=[float("nan")])
        assert vector[0] == pytest.approx(geometric)

    def test_an_absolute_map_ignores_clearness(self):
        model = ShadingModel.fit({"only": shade(full_sky(1.0), 110.0, 15.0, 0.2)})
        geometric = model.factor("only", 110.0, 16.0)
        assert geometric < 0.5
        assert model.factor("only", 110.0, 16.0, beam=0.0) == pytest.approx(geometric)

    def test_a_loss_seen_at_half_beam_is_stored_as_the_clear_day_loss(self):
        """The inversion: partial-beam residuals must not be double-blended.

        Sixty days of half-beam sky observe the shaded cell at 0.75.  Stored
        as-is, apply-time blending would scale that by the beam share *again*
        and predict 0.875 for the very weather it was measured in.  Inverted
        to the clear-day 0.5 first, the round trip returns 0.75.
        """
        model = ShadingModel.fit(
            joint_sky(
                {
                    "a": {"level": 1.0, "shade": {(110.0, 35.0): 0.5}},
                    "b": {"level": 1.0},
                },
                days=60,
                beam=0.5,
            )
        )
        # Clear-day figure, held back only by the shrinkage.
        assert model.factor("a", 110.0, 36.0) < 0.65
        # The round trip: applied at the beam share it was learned at, the
        # prediction lands on what was actually measured there.
        assert model.factor("a", 110.0, 36.0, beam=0.5) == pytest.approx(
            0.75, abs=0.06
        )

    def test_disjoint_histories_cannot_pretend_to_difference(self):
        """Two strings that never observed together have no common moments.

        "Differential" over disjoint data would read one string's private
        weather as shade; without enough shared epochs the fit must say so
        and fall back to the absolute per-string maps.
        """
        first = joint_sky({"a": {"level": 1.0}}, days=10)
        second = joint_sky(
            {"b": {"level": 1.0}}, days=10, epoch0=SUMMER + 100 * DAY
        )
        model = ShadingModel.fit({"a": first["a"], "b": second["b"]})
        assert model.method == "absolute"
        assert model.level("a") is None

    def test_a_string_nobody_could_cross_check_keeps_its_absolute_map(self):
        """Mixed overlap: two siblings difference, the third stands alone.

        The lone string must get the absolute map it would have had on its
        own -- an empty differential map would read "no shade" on exactly
        the string with the un-cross-checkable history.  And because its
        envelope already averages the weather in, no beam blending on top.
        """
        joint = joint_sky(
            {
                "a": {"level": 1.0, "shade": {(110.0, 35.0): 0.5}},
                "b": {"level": 1.0},
            }
        )
        alone = joint_sky(
            {"c": {"level": 1.0, "shade": {(110.0, 35.0): 0.3}}},
            days=40,
            epoch0=SUMMER + 200 * DAY,
        )
        model = ShadingModel.fit(
            {"a": joint["a"], "b": joint["b"], "c": alone["c"]},
            now_ts=SUMMER + 241 * DAY,
        )
        assert model.method == "differential"
        assert model.method_of("a") == "differential"
        assert model.method_of("c") == "absolute"
        assert model.level("c") is None
        # The lone shadow survives via the absolute path...
        shaded_c = model.factor("c", 110.0, 36.0)
        assert shaded_c < 0.5
        # ...and is not beam-blended: the envelope already holds the weather.
        assert model.factor("c", 110.0, 36.0, beam=0.3) == pytest.approx(shaded_c)
        # The differencing pair is untouched by the bystander.
        assert model.factor("a", 110.0, 36.0) < 0.65
        assert model.factor("b", 110.0, 36.0) == pytest.approx(1.0, abs=0.05)

    def test_legacy_five_field_rows_still_fit_jointly(self):
        """Rows written before v4 carry no watts and no beam share.

        They are down-weighted by the agnostic default but never *inverted*
        by it -- dividing a clear-day residual by an assumed half beam would
        double every shadow in the store for the first weeks after an
        upgrade.
        """
        rows = joint_sky(
            {
                "shaded": {"level": 1.0, "shade": {(110.0, 35.0): 0.4}},
                "clear": {"level": 1.0},
            },
            days=60,
        )
        legacy = {
            string_id: [row[:5] for row in string_rows]
            for string_id, string_rows in rows.items()
        }
        model = ShadingModel.fit(legacy)
        assert model.method == "differential"
        shaded = model.factor("shaded", 110.0, 36.0)
        # Legacy ignorance understates: with the beam unknown, part of the
        # shadow leaks into the moment term and the observed 0.4 surfaces as
        # roughly 0.6.  The bound to hold is the *other* side -- the naive
        # inversion would have doubled it to ~0.16, and that must not happen.
        assert 0.3 < shaded < 0.7
        assert model.factor("clear", 110.0, 36.0) == pytest.approx(1.0, abs=0.05)

    def test_the_grid_keeps_the_raw_ratio_next_to_the_loss(self):
        """A residual alone cannot say what the string actually did there."""
        model = ShadingModel.fit(
            joint_sky(
                {
                    "victron": {"level": 1.5, "shade": {(110.0, 35.0): 0.5}},
                    "garden": {"level": 1.3},
                }
            )
        )
        cells = {
            (cell["az"], cell["el"]): cell
            for cell in model.grid("victron")
            if cell["season"] is None
        }
        shaded = cells[(110.0, 35.0)]
        open_sky = cells[(200.0, 35.0)]
        assert shaded["ratio"] == pytest.approx(0.75, abs=0.05)
        assert open_sky["ratio"] == pytest.approx(1.5, abs=0.1)
        assert shaded["loss"] > 30.0
        assert open_sky["loss"] == pytest.approx(0.0, abs=2.0)
