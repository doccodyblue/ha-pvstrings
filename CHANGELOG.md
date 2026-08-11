# Changelog

## 1.2.2

**Update immediately if you are on 1.1.0, 1.2.0 or 1.2.1 — those versions
collect almost nothing.**

### Fixed

- **The collector flushed the wrong five-minute window.** The fix in 1.1.0 for
  a delayed-event-loop problem was off by one interval: it persisted the window
  that was *starting* rather than the one that had just closed. Every interval
  was therefore written with roughly one second of data, coverage collapsed to
  about 1/300, and every hour was then discarded as unusable.

  Nothing failed visibly. The collector's own counters kept reporting healthy
  sample rates and thousands of events, while `observations_used` sat at zero.

  Data recorded under the affected versions cannot be recovered; the intervals
  contain what was actually captured. Measurements are unaffected from the
  moment you upgrade.

  The boundary arithmetic is now a plain function in `core/aggregate.py` with
  tests covering on-time, late and very late callbacks.

## 1.2.1

### Fixed

- The two sensors added in 1.2.0 were declared in the translation files as bare
  strings instead of `{"name": ...}`, so Home Assistant never found them and
  fell back to the device-class name. Both ended up called "Power", collided,
  and one was given a `_2` suffix. Anyone who installed 1.2.0 has
  `..._power` and `..._power_2`; see below.

**If you installed 1.2.0:** delete those two entities under Settings →
Devices & Services → Entities, then reload the integration. They come back with
correct names and ids. The entity registry keeps whatever id an entity was
first given, so this cannot fix itself.

## 1.2.0

### Added

- **Plant-level power and potential sensors.** The per-string view is what the
  integration is *for*, but the sum is what you act on when deciding whether to
  run an appliance now. `Power now` follows the string sensors directly rather
  than the fifteen-minute coordinator cycle, because a stale power reading is
  useless for that decision. `Potential this hour` is the matching forecast.
- Power readings are normalised like every other quantity, so a string sensor
  reporting kW is no longer summed as if it were watts.

`Power now` sums the configured strings rather than reading a house meter --
that keeps it comparable with the forecast, which covers exactly those strings
and nothing else.

## 1.1.0

**Upgrade recommended.** 1.0.0 contains five defects found in an independent
review, three of which quietly corrupt what the model learns.

### Fixed

- **The learning cursor stepped over a backlog.** It advanced to the present
  after processing at most `max_hours`, so any downtime longer than that window
  left those hours unlearned permanently. It now walks forward from the cursor
  in bounded chunks; a week of downtime clears over a few hourly cycles.
- **Censoring was irreversible.** A five-minute interval marked `lower_bound`
  never returned to `measured`, even though the binding verdict is recomputed
  every cycle. One bad physics estimate therefore censored that interval for
  good. `reconstructed` is still left alone.
- **The GHI bias model scored hindsight.** Open-Meteo's `past_days` returns
  rows for hours already over; those have a negative horizon and are analyses,
  not forecasts. They stay usable as the yardstick but are no longer scored,
  which was flattering the 0-6 h bucket.
- **A delayed event loop could drop a five-minute window.** The collector
  derived the interval boundary from the current clock instead of the time the
  flush was scheduled for. It now flushes the scheduled boundary and catches up
  any missed in between.
- **Changing only the temperature coefficient did not open a new geometry
  period,** so the form and the database disagreed while the forecast kept
  using the old value.

### Added

- Forecast horizon extended from 48 to 72 hours, with a new
  **Forecast day after tomorrow** sensor. Useful for side-by-side comparison
  with other forecast services.

## 1.0.0

First release. Per-string PV forecasting: pvlib physics plus a hierarchical
log-ratio correction of the residual, geometry as a validity history,
curtailment detection per group and per tracker, and accuracy reported
separately for uncensored and all hours.
