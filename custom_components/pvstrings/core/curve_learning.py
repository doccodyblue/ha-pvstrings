"""Stage B: correct the datasheet curve with what the plant actually does.

The datasheet curve (stage A) is a prior, never a competitor.  Learning
moves each support point towards the measured efficiency at that load,
bounded on three sides:

* a hard cap around the prior, so one bad sensor day cannot rewrite the
  curve;
* a minimum of evidence per point, below which the prior simply stands --
  a half-populated curve is worse than the datasheet, because its gaps
  are where the interpolation runs;
* recency weighting, so ageing hardware is followed rather than averaged
  with its younger self.

What is deliberately not learned here: intervals the censoring marked,
the clipped region (the ceiling is its own parameter, not a curve point)
and the standby floor, where the inverter's own consumption dominates
and every ratio is about the load, not the conversion.

Known property, bounded rather than removed: support points shrink
independently, so where two neighbours pull opposite ways with very
different evidence, the interpolated value between them can sit outside
both the pure prior and the pure measurement.  It stays inside the cap
regardless, and it needs a *credible* neighbour measuring the opposite
way -- which an efficiency curve does not do: alternating residuals are
noise, noise is thin, and thin barely moves.  Should real data ever show
alternating residuals with weight behind both, the fix is neighbour
smoothing of the residual field, the way the sky map borrows from
adjacent cells.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

#: Load fractions the curve is anchored at, in percent of the reference
#: power.  Dense at the bottom because that is where efficiency actually
#: moves -- and where a good part of the yearly energy sits, in mornings,
#: evenings and overcast days.  Provisional: revisit once a few clear days
#: of evidence show where the pairs really land.
LOAD_BUCKETS: tuple[float, ...] = (2.0, 5.0, 10.0, 20.0, 35.0, 50.0, 75.0, 100.0)

#: Below this the inverter's own consumption dominates the ratio.
STANDBY_FLOOR_PCT = 1.0

#: Output within this much of the rating is clipping, not conversion.
CLIP_GUARD = 0.98

#: Evidence halves over this span.  Long, because a conversion curve is a
#: property of the hardware, not of the weather.
RECENCY_HALFLIFE_DAYS = 365.0

#: Reserved pseudo-load under which the highest observed load is stored,
#: so it survives a restart alongside the support points.
MAX_LOAD_KEY = -1.0


@dataclass(frozen=True, slots=True)
class Bin:
    """One support point after learning."""

    #: The applied value: the prior, moved towards ``measured`` in
    #: proportion to how much evidence stands behind it.
    eta: float
    n_eff: float
    #: What the prior said, for the diagnostics comparison.
    prior: float
    #: Materially evidence-driven, i.e. past the half-way point.  Not a
    #: gate -- the value moves continuously -- but the honest label for
    #: "this point is now mostly measurement".
    learned: bool
    #: The raw weighted mean before shrinking, so a point can be watched
    #: forming long before it carries the curve.  ``None`` without samples.
    measured: float | None = None
    #: Weighted standard deviation of the samples: how settled the point is.
    spread: float | None = None


@dataclass(frozen=True, slots=True)
class LearnedCurve:
    """A curve plus the evidence behind it."""

    bins: dict[float, Bin] = field(default_factory=dict)
    #: Highest load fraction ever observed, in percent.  A 1.4 kWp array on
    #: a 1600 W inverter physically cannot pass ~87 %, so its top support
    #: points can never fill -- and a readiness figure measured against
    #: them would sit below 100 % for ever and read as "never finished".
    max_load_pct: float = 0.0

    @property
    def reachable(self) -> tuple[float, ...]:
        """Support points this plant can actually reach.

        Derived from what was observed, not from configuration: an
        oversized generator reaches every point, a modest one does not,
        and neither has to say so anywhere.
        """
        if self.max_load_pct <= 0:
            return tuple(self.bins)
        highest = max(
            (load for load in self.bins if load <= self.max_load_pct),
            default=min(self.bins, default=0.0),
        )
        return tuple(load for load in sorted(self.bins) if load <= highest)

    @property
    def coverage(self) -> float:
        """Share of the *reachable* support points that are evidence-driven."""
        reachable = self.reachable
        if not reachable:
            return 0.0
        moved = sum(1 for load in reachable if self.bins[load].learned)
        return round(moved / len(reachable), 3)

    @property
    def any_learned(self) -> bool:
        return any(b.learned for b in self.bins.values())

    @property
    def any_evidence(self) -> bool:
        return any(b.n_eff > 0 for b in self.bins.values())

    def points(self) -> tuple[tuple[float, float], ...]:
        """``((load_pct, eta), ...)`` for the interpolator."""
        return tuple(sorted((load, b.eta) for load, b in self.bins.items()))

    def as_dict(self) -> dict[str, object]:
        """The shape the dashboard asked for."""
        reachable = set(self.reachable)
        return {
            "coverage": self.coverage,
            "max_load": round(self.max_load_pct / 100, 3),
            "bins": {
                f"{load / 100:.2f}": {
                    "eta": round(b.eta, 4),
                    "n_eff": round(b.n_eff, 1),
                    "prior": round(b.prior, 4),
                    "learned": b.learned,
                    "measured": None if b.measured is None else round(b.measured, 4),
                    "spread": None if b.spread is None else round(b.spread, 4),
                    "reachable": load in reachable,
                }
                for load, b in sorted(self.bins.items())
            },
        }


def _nearest_bucket(load_pct: float) -> float:
    return min(LOAD_BUCKETS, key=lambda edge: abs(edge - load_pct))


def _recency(ts_utc: float, now_ts: float) -> float:
    age_days = max(0.0, (now_ts - ts_utc) / 86400.0)
    return 0.5 ** (age_days / RECENCY_HALFLIFE_DAYS)


def _prior_at(prior: Sequence[tuple[float, float]], load_pct: float) -> float:
    """Linear interpolation on the prior, clamped at both ends."""
    if load_pct <= prior[0][0]:
        return prior[0][1]
    if load_pct >= prior[-1][0]:
        return prior[-1][1]
    for (lo_l, lo_e), (hi_l, hi_e) in zip(prior, prior[1:]):
        if lo_l <= load_pct <= hi_l:
            span = hi_l - lo_l
            return lo_e if span <= 0 else lo_e + (hi_e - lo_e) * (load_pct - lo_l) / span
    return prior[-1][1]


def fit_curve(
    pairs: Iterable[tuple[float, float, float, float]],
    prior: Sequence[tuple[float, float]],
    reference_w: float,
    now_ts: float,
    max_deviation_pp: float = 5.0,
    min_samples: float = 50.0,
) -> LearnedCurve:
    """Fit support points from ``(ts, in_w, out_w, coverage)`` measurements.

    ``reference_w`` turns watts into a load fraction: the inverter's AC
    rating for the inverter stage, the string's kWp for an MPPT.  Points
    without enough evidence keep the prior, so the returned curve is a
    blend and always complete.
    """
    if not prior or reference_w <= 0:
        return LearnedCurve()

    buckets: dict[float, list[tuple[float, float]]] = {}
    max_load = 0.0
    for ts_utc, in_w, out_w, coverage in pairs:
        if in_w <= 0 or out_w < 0 or coverage <= 0:
            continue
        load_pct = in_w / reference_w * 100.0
        if load_pct < STANDBY_FLOOR_PCT:
            continue
        max_load = max(max_load, load_pct)
        if out_w >= reference_w * CLIP_GUARD:
            # At the ceiling the output stopped following the input; that
            # is the clip level, and folding it in would bend the top of
            # the curve downwards for a reason that is not conversion.
            continue
        efficiency = out_w / in_w
        if not 0.3 < efficiency <= 1.05:
            # Outside this the pair is a unit or sign problem, not a loss.
            continue
        weight = coverage * _recency(ts_utc, now_ts)
        if weight <= 0:
            continue
        buckets.setdefault(_nearest_bucket(load_pct), []).append(
            (efficiency, weight)
        )

    bins: dict[float, Bin] = {}
    cap = max_deviation_pp / 100.0
    for load in LOAD_BUCKETS:
        prior_eta = _prior_at(prior, load)
        samples = buckets.get(load, [])
        evidence = sum(weight for _eta, weight in samples)
        if evidence <= 0:
            bins[load] = Bin(
                eta=prior_eta, n_eff=0.0, prior=prior_eta, learned=False
            )
            continue
        measured = sum(eta * weight for eta, weight in samples) / evidence
        variance = (
            sum(weight * (eta - measured) ** 2 for eta, weight in samples)
            / evidence
        )
        # Shrunk towards the prior by how much this point knows, instead of
        # switched at a threshold.  A hard switch put a step between a
        # learned support point and its unlearned neighbour -- a kink in
        # the interpolated curve that no inverter has, produced purely by
        # the two points coming from different sources.  With shrinkage a
        # point nobody measured stays exactly on the datasheet, and one
        # measured all summer stands on its own; everything in between is
        # continuous, which is what lets the top of the curve keep the
        # datasheet on a plant that can never reach it.
        weight = evidence / (evidence + max(min_samples, 1.0))
        moved = prior_eta + (measured - prior_eta) * weight
        bounded = min(max(moved, prior_eta - cap), prior_eta + cap)
        bins[load] = Bin(
            eta=min(bounded, 1.0),
            n_eff=round(evidence, 2),
            prior=prior_eta,
            learned=evidence >= min_samples,
            measured=measured,
            spread=variance ** 0.5,
        )
    return LearnedCurve(bins=bins, max_load_pct=round(max_load, 2))


def to_rows(curves: Mapping[str, LearnedCurve]) -> dict[str, tuple[float, float]]:
    """Learned points as ``{scope|stage|load: (measured, n_eff)}``.

    The *measured* value is stored, not the shrunk one: shrinking depends
    on the configured evidence constant, and baking today's setting into
    the database would make a later change to it unnoticeable.  Points
    with no evidence are left out entirely -- a stored prior would freeze
    today's datasheet in and survive a correction of the datasheet itself.
    """
    out: dict[str, tuple[float, float]] = {}
    for key, curve in curves.items():
        for load, bin_ in curve.bins.items():
            if bin_.n_eff > 0 and bin_.measured is not None:
                out[f"{key}|{load:g}"] = (bin_.measured, bin_.n_eff)
        if curve.max_load_pct > 0:
            # Under a reserved load so it round-trips with the points.
            # Rebuilding it from which buckets happen to hold samples is
            # lossy -- buckets take the nearest edge, so a plant peaking at
            # 90 % stores under the 100 % edge and would come back claiming
            # it reaches full load.
            out[f"{key}|{MAX_LOAD_KEY:g}"] = (curve.max_load_pct, 0.0)
    return out


def from_rows(
    rows: Mapping[str, tuple[float, float]],
    priors: Mapping[str, Sequence[tuple[float, float]]],
    max_deviation_pp: Mapping[str, float] | None = None,
    default_deviation_pp: float = 5.0,
    min_samples: Mapping[str, float] | None = None,
    default_min_samples: float = 50.0,
) -> dict[str, LearnedCurve]:
    """Rebuild curves from storage, filling unlearned points from the prior.

    Stored points carry no memory of which prior they were fitted against,
    so they are re-capped here against the *current* one.  Swapping the
    inverter model or editing a custom curve therefore cannot leave a point
    further from the datasheet than the cap allows -- which is the whole
    promise of the cap, and it has to survive a restart to mean anything.
    """
    grouped: dict[str, dict[float, tuple[float, float]]] = {}
    for key, (eta, n_eff) in rows.items():
        scope_key, _, load_text = key.rpartition("|")
        try:
            load = float(load_text)
        except ValueError:
            continue
        grouped.setdefault(scope_key, {})[load] = (eta, n_eff)

    out: dict[str, LearnedCurve] = {}
    # Every configured scope gets a curve, evidence or not: a dashboard has
    # to tell "switched on, collecting" from "not configured", and an
    # absent entry says the second.  Before this, a restart with nothing
    # stored yet looked exactly like learning being off.
    for scope_key in priors:
        grouped.setdefault(scope_key, {})
    for scope_key, points in grouped.items():
        prior = priors.get(scope_key)
        if not prior:
            continue
        stored_max = points.pop(MAX_LOAD_KEY, None)
        cap = (
            (max_deviation_pp or {}).get(scope_key, default_deviation_pp)
        ) / 100.0
        threshold = (min_samples or {}).get(scope_key, default_min_samples)
        bins: dict[float, Bin] = {}
        max_load = 0.0
        for load in LOAD_BUCKETS:
            prior_eta = _prior_at(prior, load)
            if load in points:
                measured, n_eff = points[load]
                max_load = max(max_load, load)
                # Re-shrunk and re-capped against the prior in force *now*:
                # neither the evidence constant nor the datasheet is frozen
                # into the stored value, so changing either takes effect
                # without waiting for a refit.
                weight = n_eff / (n_eff + max(threshold, 1.0))
                moved = prior_eta + (measured - prior_eta) * weight
                bounded = min(
                    max(moved, prior_eta - cap), min(prior_eta + cap, 1.0)
                )
                bins[load] = Bin(
                    eta=bounded,
                    n_eff=n_eff,
                    prior=prior_eta,
                    learned=n_eff >= threshold,
                    measured=measured,
                )
            else:
                bins[load] = Bin(
                    eta=prior_eta, n_eff=0.0, prior=prior_eta, learned=False
                )
        out[scope_key] = LearnedCurve(
            bins=bins,
            max_load_pct=stored_max[0] if stored_max else max_load,
        )
    return out
