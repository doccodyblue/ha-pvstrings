"""Is the integration working, or has it merely not crashed?

Those are different questions, and until now only the second one had an
answer.  A quiet log meant nothing had raised; it said nothing about whether
any data was being captured or anything learned from it.  Two separate
installations spent days in exactly that state -- one collecting fine but
learning nothing because the physics had collapsed, one apparently collecting
nothing at all -- and in both cases the only outward sign was a sensor reading
zero that nobody could interpret.

The rules here are deliberately slow.  A single quiet interval is a restart,
an inverter waking up, a cloud passing over a flaky WiFi link; none of that is
worth telling anybody about.  Something is only worth reporting once it has
persisted long enough that it will not fix itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

#: Above this the sun is high enough that a healthy plant must be producing,
#: so silence from every string means the capture path is broken rather than
#: the sky being dark.
DAYLIGHT_ELEVATION_DEG = 10.0

#: Mean coverage below this counts as "nothing is arriving".
DEAD_COVERAGE = 0.1

#: Consecutive updates of silence before saying so.  The coordinator updates
#: every few minutes, so this is a quarter of an hour or more -- long enough
#: to ride out a restart, short enough to catch the same afternoon.
DEAD_COVERAGE_UPDATES = 5

#: Consecutive learn cycles that folded daylight hours and learned nothing
#: from any of them.  Hourly, so this is most of a working day.
BARREN_CYCLES = 5


@dataclass(slots=True)
class Health:
    """Rolling judgement on whether the integration is doing its job."""

    quiet_updates: int = 0
    barren_cycles: int = 0
    _reported: set[str] = field(default_factory=set)

    # -- capture ---------------------------------------------------------- #

    def observe_coverage(
        self, coverage: Mapping[str, float], sun_elevation_deg: float
    ) -> str | None:
        """Judge one coordinator update.  Returns a problem to report, once."""
        if sun_elevation_deg < DAYLIGHT_ELEVATION_DEG or not coverage:
            # Darkness explains silence, and so does having no strings yet.
            self.quiet_updates = 0
            return self._clear("no_capture")

        mean = sum(coverage.values()) / len(coverage)
        if mean >= DEAD_COVERAGE:
            self.quiet_updates = 0
            return self._clear("no_capture")

        self.quiet_updates += 1
        if self.quiet_updates < DEAD_COVERAGE_UPDATES:
            return None
        return self._report("no_capture")

    # -- learning --------------------------------------------------------- #

    def observe_learn(
        self, stats: Mapping[str, object], learning_enabled: bool = True
    ) -> str | None:
        """Judge one learn cycle.

        Barren means "had something to learn from and learned nothing", which
        is narrower than it first appears.  A cycle that folded no hours is
        idle -- that happens whenever one runs twice inside the same hour.  A
        cycle that folded only night hours had nothing learnable in it, and
        since the cycle runs hourly around the clock there are eight of those
        every night: judging on folded rows alone would raise the alarm before
        breakfast, every single day.  And a plant with learned correction
        switched off is doing exactly what it was told.
        """
        if not learning_enabled:
            self.barren_cycles = 0
            return self._clear("not_learning")
        if int(stats.get("hours_materialised") or 0) <= 0:
            return None

        used = int(stats.get("observations_used") or 0)
        reasons: Mapping[str, int] = stats.get("skipped_because") or {}  # type: ignore[assignment]
        night = int(reasons.get("night", 0))
        rejected = sum(
            int(count) for reason, count in reasons.items() if reason != "night"
        )
        # Folded, and every row that got as far as being judged was darkness:
        # nothing to learn from and nothing wrong.  The ``night`` test has to
        # be there.  Without it, a cycle that folded rows and judged *none* of
        # them reads the same way -- and that is not a quiet night, it is the
        # cycle giving up before it got there, which is what happens when the
        # weather source has gone away and there is no irradiance to run the
        # physics against.  That stall is exactly what this warning is for.
        if night and used + rejected == 0:
            return None
        if used > 0:
            self.barren_cycles = 0
            return self._clear("not_learning")

        self.barren_cycles += 1
        if self.barren_cycles < BARREN_CYCLES:
            return None
        return self._report("not_learning")

    # -- edge tracking ---------------------------------------------------- #

    def _report(self, problem: str) -> str | None:
        """Report a problem on its first appearance and then stay quiet."""
        if problem in self._reported:
            return None
        self._reported.add(problem)
        return problem

    def _clear(self, problem: str) -> None:
        """A recovered problem may be reported again if it comes back."""
        self._reported.discard(problem)
        return None

    @property
    def active(self) -> frozenset[str]:
        return frozenset(self._reported)


def learn_summary(stats: Mapping[str, object]) -> str:
    """One line describing what a learn cycle actually did.

    Written to be readable in a log without a decoder ring, and to make the
    difference between "nothing to do" and "did nothing" visible at a glance.
    """
    folded = int(stats.get("hours_materialised") or 0)
    used = int(stats.get("observations_used") or 0)
    skipped = int(stats.get("observations_skipped") or 0)
    if not folded:
        return "nothing new to fold"

    parts = [f"{folded} hourly rows folded", f"{used} observations learned"]
    if skipped:
        reasons = stats.get("skipped_because") or {}
        detail = ", ".join(f"{key} {value}" for key, value in sorted(reasons.items()))
        parts.append(f"{skipped} skipped ({detail})" if detail else f"{skipped} skipped")
    for label, key in (
        ("shading", "shading_observations"),
        ("irradiance bias", "bias_observations"),
        ("censored", "censored_hours"),
        ("irradiance hours rejected", "ghi_hours_rejected"),
    ):
        value = int(stats.get(key) or 0)
        if value:
            parts.append(f"{value} {label}")
    return ", ".join(parts)
