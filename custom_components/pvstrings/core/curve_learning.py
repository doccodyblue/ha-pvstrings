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


@dataclass(frozen=True, slots=True)
class Bin:
    """One support point after learning."""

    eta: float
    n_eff: float
    #: What the prior said, for the diagnostics comparison.
    prior: float
    #: Did the evidence actually move this point, or does the prior stand?
    learned: bool


@dataclass(frozen=True, slots=True)
class LearnedCurve:
    """A curve plus the evidence behind it."""

    bins: dict[float, Bin] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        """Share of support points that evidence has actually moved."""
        if not self.bins:
            return 0.0
        moved = sum(1 for b in self.bins.values() if b.learned)
        return round(moved / len(self.bins), 3)

    @property
    def any_learned(self) -> bool:
        return any(b.learned for b in self.bins.values())

    def points(self) -> tuple[tuple[float, float], ...]:
        """``((load_pct, eta), ...)`` for the interpolator."""
        return tuple(sorted((load, b.eta) for load, b in self.bins.items()))

    def as_dict(self) -> dict[str, object]:
        """The shape the dashboard asked for."""
        return {
            "coverage": self.coverage,
            "bins": {
                f"{load / 100:.2f}": {
                    "eta": round(b.eta, 4),
                    "n_eff": round(b.n_eff, 1),
                    "prior": round(b.prior, 4),
                    "learned": b.learned,
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
    for ts_utc, in_w, out_w, coverage in pairs:
        if in_w <= 0 or out_w < 0 or coverage <= 0:
            continue
        load_pct = in_w / reference_w * 100.0
        if load_pct < STANDBY_FLOOR_PCT:
            continue
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
        if evidence < min_samples:
            bins[load] = Bin(
                eta=prior_eta, n_eff=round(evidence, 2), prior=prior_eta,
                learned=False,
            )
            continue
        measured = sum(eta * weight for eta, weight in samples) / evidence
        bounded = min(max(measured, prior_eta - cap), prior_eta + cap)
        bins[load] = Bin(
            eta=min(bounded, 1.0),
            n_eff=round(evidence, 2),
            prior=prior_eta,
            learned=True,
        )
    return LearnedCurve(bins=bins)


def to_rows(curves: Mapping[str, LearnedCurve]) -> dict[str, tuple[float, float]]:
    """Learned points as ``{scope|stage|load: (eta, n_eff)}`` for the store.

    Only the points evidence actually moved are persisted: a stored prior
    would freeze today's datasheet into the database and survive a later
    correction of the datasheet itself.
    """
    out: dict[str, tuple[float, float]] = {}
    for key, curve in curves.items():
        for load, bin_ in curve.bins.items():
            if bin_.learned:
                out[f"{key}|{load:g}"] = (bin_.eta, bin_.n_eff)
    return out


def from_rows(
    rows: Mapping[str, tuple[float, float]],
    priors: Mapping[str, Sequence[tuple[float, float]]],
    max_deviation_pp: Mapping[str, float] | None = None,
    default_deviation_pp: float = 5.0,
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
    for scope_key, points in grouped.items():
        prior = priors.get(scope_key)
        if not prior:
            continue
        cap = (
            (max_deviation_pp or {}).get(scope_key, default_deviation_pp)
        ) / 100.0
        bins: dict[float, Bin] = {}
        for load in LOAD_BUCKETS:
            prior_eta = _prior_at(prior, load)
            if load in points:
                eta, n_eff = points[load]
                bounded = min(
                    max(eta, prior_eta - cap), min(prior_eta + cap, 1.0)
                )
                bins[load] = Bin(
                    eta=bounded, n_eff=n_eff, prior=prior_eta, learned=True
                )
            else:
                bins[load] = Bin(
                    eta=prior_eta, n_eff=0.0, prior=prior_eta, learned=False
                )
        out[scope_key] = LearnedCurve(bins=bins)
    return out
