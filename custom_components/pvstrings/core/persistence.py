"""Nowcast: die eigene Einstrahlungsmessung in die nächsten Stunden tragen.

The weather source only corrects a running day when its next run lands, which
cost Andy's plant 1.4 kWh on 2026-08-22 -- the sensor saw the sun break through
at 11:00, the forecast followed at 13:02, and the best hour was gone.

Calibrated against 11 days of five-minute data (see plan, step 1).  Two results
shaped this module:

* **kt persistence beats ratio persistence.**  Carrying ``measured/forecast``
  forward multiplies two noisy quantities and is *worse than doing nothing*
  beyond 45 min.  Holding the measured clearness index and letting the
  clear-sky curve supply the shape wins at every horizon (MAE -38 % at 15 min,
  -27 % at 30 min, -11 % at 60 min) and stays useful to ~90 min.
* **The half-life depends on the sky, not on the season.**  A calm sky carries
  70 min, a broken one 31 min.  Splitting on the spread of the recent error
  makes winter stratus and summer cumulus fall out correctly without anyone
  configuring anything.

Pure: no store, no Home Assistant, no clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

#: Trailing window the clearness index is read from.  10-20 min measured
#: equivalent; longer windows smear the very transition we are trying to catch.
WINDOW_SECONDS = 900

#: Beyond this the measurement carries nothing (kt turns harmful at ~120 min).
#: The weight is already tiny here; the cut is a guarantee, not a knee.
REACH_SECONDS = 7200

#: Half-lives per sky regime, from the correlation decay of the log error.
HALFLIFE_CALM_S = 4200.0
HALFLIFE_BROKEN_S = 1860.0

#: Median spread of log(measured/forecast) over 30 min -- the calm/broken line.
SPREAD_SPLIT = 0.143

#: Below this clear-sky irradiance every ratio explodes; dawn and dusk are noise.
CS_FLOOR_WM2 = 50.0

#: Cloud enhancement is real but not sustained.  Also the ceiling applied to
#: the blended result.
KT_MAX = 1.1

#: Fewer usable intervals than this is not a measurement, it is a sample.
MIN_INTERVALS = 3

#: Bias-model evidence at which the nowcast is believed by half.  Until the
#: bias buckets have converged, the forecast term and the measured term are
#: anchored to different scales (a lux sensor reads its own units), and blending
#: them hard produces a step across the horizon rather than a correction.
#:
#: Scaled to what ``n_eff`` actually reaches: it is an irradiance-weighted,
#: decaying count, not a sample tally, and on a mature plant it saturates
#: around 10-16 at midday and 1-2 near the edges of the day.  A larger constant
#: throttles the feature permanently rather than only while it is young -- at
#: 20 even the best-evidenced hour would have stayed below half weight.
BIAS_EVIDENCE_K = 3.0

REASON_NO_SOURCE = "no_source"
REASON_NO_MEASUREMENT = "no_measurement"
REASON_TOO_DARK = "too_dark"
REASON_THIN = "thin_window"
REASON_STALE = "stale"
REASON_FROZEN = "frozen_sensor"
REASON_LEARNING_OFF = "learning_off"


@dataclass(frozen=True, slots=True)
class SkyState:
    """What the last quarter hour actually looked like."""

    #: Measured clearness index, clamped to ``KT_MAX``.
    kt: float
    #: Spread of the log error over the window -- how broken the sky is.
    #: ``None`` when no forecast row covered the window, in which case the
    #: regime is unknown and the short half-life applies.
    spread: float | None
    #: Usable five-minute intervals behind ``kt``.
    intervals: int
    #: Half-life implied by ``spread``, in seconds.
    halflife_s: float
    #: Extra damping in [0, 1] while the bias model is still thin.
    trust: float = 1.0

    def weight(self, horizon_s: float) -> float:
        """How much of the measurement survives ``horizon_s`` into the future.

        Zero for anything already past: ``forecast()`` runs from the start of
        the local day, so the series carries negative horizons, and
        ``0.5 ** (negative / halflife)`` would *amplify* the past rather than
        leave it alone.  The elapsed part of the day must come out bit
        identical or the accuracy scoring silently grades a hindcast.
        """
        if horizon_s <= 0.0 or horizon_s >= REACH_SECONDS:
            return 0.0
        return self.trust * 0.5 ** (horizon_s / self.halflife_s)


def halflife_for(spread: float | None) -> float:
    """Calm sky carries further than a broken one.

    An unknown regime gets the short half-life, not the long one: without
    evidence the conservative claim is that the sky is about to change.
    """
    if spread is None:
        return HALFLIFE_BROKEN_S
    return HALFLIFE_CALM_S if spread <= SPREAD_SPLIT else HALFLIFE_BROKEN_S


def looks_frozen(measured: np.ndarray) -> bool:
    """A sensor repeating one value to the bit has stopped measuring.

    The collector's watchdog stamps ``hass.states.get()`` with the time it
    sampled, not with the age of the state, so an entity that quietly stops
    updating keeps producing fresh-looking rows holding a dead value.  Real
    irradiance never repeats exactly across a quarter hour -- the sun moves.
    Same reasoning as the flatness guard on the conversion curves.
    """
    finite = measured[np.isfinite(measured)]
    if finite.size < MIN_INTERVALS:
        return False
    # Darkness legitimately sits at a constant zero.
    if float(np.max(finite)) <= CS_FLOOR_WM2:
        return False
    return bool(np.all(finite == finite[0]))


def bias_trust(n_eff: float) -> float:
    """Shrinkage on the bias model's evidence, the usual ``n/(n+k)``."""
    if n_eff <= 0.0:
        return 0.0
    return float(n_eff / (n_eff + BIAS_EVIDENCE_K))


def sky_state(
    measured: np.ndarray,
    forecast: np.ndarray,
    clearsky: np.ndarray,
    bias_evidence: float = 0.0,
) -> SkyState | None:
    """Read the clearness index and the sky's restlessness off the window.

    ``forecast`` is only used for the spread -- the regime signal is the
    *disagreement* between source and sensor, which is what actually predicts
    how long the current state will hold.  ``kt`` itself never touches it, so a
    weak forecast cannot drag the level around.
    """
    usable = (
        np.isfinite(measured)
        & np.isfinite(clearsky)
        & (clearsky > CS_FLOOR_WM2)
        & (measured >= 0.0)
    )
    if int(usable.sum()) < MIN_INTERVALS:
        return None

    kt_all = measured[usable] / clearsky[usable]
    kt = float(np.median(kt_all))
    if not math.isfinite(kt) or kt <= 0.0:
        return None

    spread: float | None = None
    fc_ok = usable & np.isfinite(forecast) & (forecast > 5.0)
    if int(fc_ok.sum()) >= MIN_INTERVALS:
        ratio = measured[fc_ok] / forecast[fc_ok]
        ratio = ratio[(ratio > 0.05) & (ratio < 5.0)]
        if ratio.size >= MIN_INTERVALS:
            spread = float(np.std(np.log(ratio)))

    return SkyState(
        kt=min(kt, KT_MAX),
        spread=spread,
        intervals=int(usable.sum()),
        halflife_s=halflife_for(spread),
        trust=bias_trust(bias_evidence),
    )


def blend(
    forecast_ghi: np.ndarray,
    clearsky_ghi: np.ndarray,
    weights: np.ndarray,
    kt: float,
) -> np.ndarray:
    """Fade from the measured clearness index back to the source's forecast.

    At ``weight = 1`` the result is the clear-sky curve scaled by what the
    sensor just saw; at ``weight = 0`` it is the forecast, untouched, to the
    last bit.  Capped at the clear-sky ceiling because no persistence argument
    justifies predicting more light than the sky can deliver.
    """
    persisted = kt * clearsky_ghi
    out = weights * persisted + (1.0 - weights) * forecast_ghi
    return np.clip(out, 0.0, clearsky_ghi * KT_MAX)
