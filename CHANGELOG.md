# Changelog

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
