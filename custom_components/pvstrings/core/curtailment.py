"""Detecting when a string was actually held back, and what to do about it.

Two things this module refuses to conflate:

* A **commanded limit** is not curtailment.  At a 1796 W limit with 600 W
  available, the measurement is an exact value, not a lower bound.
* Curtailment hits a **group**, shading hits a **string**.  Curtailment is not
  reproducible at the same sun position, shading is.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Sequence

from .quality import VALUE_LOWER_BOUND, VALUE_MEASURED, VALUE_RECONSTRUCTED

#: How close to the limit counts as "running into the wall".
BINDING_MEASURED_MARGIN = 0.97

#: How far physics has to exceed the limit before we believe it is binding.
BINDING_PHYSICS_MARGIN = 1.05

#: Gating for peer reconstruction.
PEER_MIN_ELEVATION_DEG = 12.0
PEER_MIN_LOAD_FRACTION = 0.10
PEER_MAX_RATIO_SPREAD = 0.35
PEER_WEIGHT = 0.35


def is_binding(
    measured_w: float | None,
    limit_commanded_w: float | None,
    physics_potential_w: float | None,
) -> bool | None:
    """Was the commanded limit actually in effect?

    ``None`` means "cannot tell" -- no limit known, or no physics estimate yet.
    That is a distinct state from ``False`` and must stay distinguishable in the
    database, otherwise the learning layer cannot tell an uncensored hour from
    an unevaluated one.
    """
    if limit_commanded_w is None or measured_w is None:
        return None
    if limit_commanded_w <= 0:
        # A zero limit means the inverter is commanded off: anything it still
        # produces is by definition censored.
        return True
    if physics_potential_w is None:
        return None
    return (
        measured_w >= limit_commanded_w * BINDING_MEASURED_MARGIN
        and physics_potential_w > limit_commanded_w * BINDING_PHYSICS_MARGIN
    )


def value_kind_for(binding: bool | None) -> str:
    return VALUE_LOWER_BOUND if binding else VALUE_MEASURED


@dataclass(frozen=True, slots=True)
class PeerSample:
    """A candidate reference string for reconstruction."""

    string_id: str
    measured_w: float
    physics_w: float
    binding: bool | None
    shaded: bool = False

    @property
    def ratio(self) -> float:
        return self.measured_w / self.physics_w if self.physics_w > 0 else 0.0


@dataclass(frozen=True, slots=True)
class Reconstruction:
    value_w: float
    weight: float
    value_kind: str
    peers: tuple[str, ...]
    reason: str


def reconstruct_from_peers(
    target_physics_w: float,
    target_nameplate_w: float,
    peers: Sequence[PeerSample],
    sun_elevation_deg: float,
    target_shaded: bool = False,
    historical_ratio: float | None = None,
) -> Reconstruction | None:
    """Estimate what a curtailed string would have produced.

    This is a weak pseudo-label, never a measurement.  All of the following must
    hold, and if they do not we return ``None`` rather than guessing:

    * the sun is high enough that the ratio is stable
    * at least one peer is demonstrably unconstrained
    * both target and peer are meaningfully loaded
    * neither is known to be shaded
    * the free peers agree with each other

    When *every* group is curtailed at the same time -- a summer midday with a
    full battery and the load covered -- there is no defensible point value at
    all.  The system then keeps forecasting physically and keeps learning the
    GHI bias, but it cannot evidence the string potential.  That is a limit of
    the method, not a bug.
    """
    if sun_elevation_deg < PEER_MIN_ELEVATION_DEG:
        return None
    if target_shaded:
        return None
    if target_physics_w < target_nameplate_w * PEER_MIN_LOAD_FRACTION:
        return None

    free = [
        peer
        for peer in peers
        if peer.binding is False
        and not peer.shaded
        and peer.physics_w > 0
        and peer.measured_w > 0
    ]
    if not free:
        return None

    ratios = [peer.ratio for peer in free]
    if len(ratios) >= 2:
        spread = max(ratios) - min(ratios)
        if spread > PEER_MAX_RATIO_SPREAD:
            return None
        ratio = statistics.median(ratios)
    else:
        ratio = ratios[0]

    if historical_ratio is not None and historical_ratio > 0:
        # Calibrate against periods when both strings were demonstrably free,
        # so a permanent difference between the strings is not read as
        # curtailment relief.
        ratio *= historical_ratio

    value = target_physics_w * ratio
    weight = PEER_WEIGHT if len(free) == 1 else min(0.5, PEER_WEIGHT + 0.05 * len(free))
    return Reconstruction(
        value_w=value,
        weight=weight,
        value_kind=VALUE_RECONSTRUCTED,
        peers=tuple(peer.string_id for peer in free),
        reason=f"median of {len(free)} free peer(s)",
    )


def group_fully_curtailed(flags: Sequence[bool | None]) -> bool:
    """True when every string we know about is censored."""
    known = [flag for flag in flags if flag is not None]
    return bool(known) and all(known)


def curtailed_fraction(flags: Sequence[bool | None]) -> float:
    known = [flag for flag in flags if flag is not None]
    if not known:
        return 0.0
    return sum(1 for flag in known if flag) / len(known)


def combine_binding(*flags: bool | None) -> bool | None:
    """Merge several censoring verdicts for the same interval.

    A string can be held back by more than one thing at once: the group's
    inverter limit, and its own tracker ceiling.  Either is enough to make the
    measurement a lower bound.  ``None`` stays ``None`` only when *nothing*
    could be evaluated -- "unknown" and "not binding" must remain
    distinguishable in the database.
    """
    if any(flag is True for flag in flags):
        return True
    if any(flag is False for flag in flags):
        return False
    return None


def group_binding(
    measured_sum_w: float | None,
    limit_w: float | None,
    physics_sum_w: float | None,
) -> bool | None:
    """Is the *group's* shared inverter limit in effect?

    The limit applies to the inverter's total output, so it has to be tested
    against the sum over the group.  Comparing one string's power against a
    limit that covers three of them simply never fires, and every clipped hour
    would then be learned as if it were free.
    """
    return is_binding(measured_sum_w, limit_w, physics_sum_w)
