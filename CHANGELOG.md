# Changelog

## 1.3.2

### Fixed

- **The irradiance sensor read `unknown` every night.** A forecast of
  0.0 W/m2 was tested for truthiness rather than against `None`, so a
  perfectly good "dark" reading looked exactly like a dead weather source.
  The power-scale lookup in the collector had the same shape and was
  tightened alongside it.

## 1.3.1

Ten defects from an independent multi-agent review, all introduced in the
changes of the previous day. **Anyone on 1.3.0 should update**: two of them
distort energy and money figures.

### Fixed

- **Unfolded hours read as zero production.** Whole hours came exclusively
  from the hourly aggregate, which only the learning cycle writes. If learning
  stalled, or after an outage longer than its 48-hour catch-up window, today's
  production and every savings figure collapsed toward zero while the
  collector kept recording correctly. Hours without an aggregate now fall back
  to the raw rows.
- **Upgrades read zero grid export.** `plant_hourly` was introduced in 1.3.0
  and never backfilled, so an existing install saw no export for its whole
  history and its lifetime savings jumped upward. The schema migration now
  folds the existing plant state.
- **Sub-hour windows were counted twice** wherever local midnight is off the
  UTC hour grid (India, Adelaide, Newfoundland). The two ragged ends of a
  window overlapped when it contained no whole hour.
- **kW power sensors were stored a thousand times too small.** The live plant
  power sensor converted units, the collector did not, so everything derived
  from stored data was wrong while the live reading looked correct.
- **Hours beyond the source's horizon became 0.00 kWh** instead of unknown, so
  the day-after-tomorrow sensor read a confident zero on any weather entity
  publishing fewer than 72 hours.
- **The collector could fall silent after a backwards clock step**, which is
  routine on hardware without an RTC once NTP corrects the boot time.
- **Adding a string reloaded the integration twice**, and the first reload ran
  before the new subentry existed.
- **Analysis-only forecast hours were never pruned** -- a SQL NULL comparison
  meant every row for such an hour survived for ever.
- **`shading_obs` lost its retention entirely** in the 1.3.0 rewrite. It now
  has a long horizon of its own (two years), since it is raw material for an
  analysis that needs a year of data.
- **A partial plant power sum was published as a valid measurement.** With
  some strings unavailable it now goes unavailable rather than letting energy
  integrations quietly accumulate a too-low total.

## 1.3.0

### Added

- **Tiered retention with compaction instead of one blanket horizon.** The
  aggregates are small and are the memory of the system; the raw rows are large
  and stop being useful once folded up. And the forecast tables are dominated
  not by target hours but by *issues* -- the same hour re-forecast every half
  hour -- of which only the closest one matters once its verification window
  has passed.

  Defaults: raw five-minute rows 90 days, forecast issues 14 days (then one per
  target hour survives), exclusions 90 days. Hourly aggregates, geometry
  history, shading observations and model state are never discarded.

  Roughly a third of the rows after two months, and it stops growing without
  bound.

- New `plant_hourly` aggregate so plant state can be condensed as well.

### Fixed

- **Lifetime totals no longer depend on rows that retention may remove.**
  `savings_total` reads production and grid flow over the whole period since
  commissioning, but both queries read the raw five-minute tables -- lowering
  the retention would have shrunk the lifetime savings silently. Whole hours
  now come from the aggregates and only the ragged ends from raw rows, and raw
  rows are only ever dropped where the corresponding aggregate row exists.

  The default retention drops from 1095 to 90 days, which was safe to do only
  because of that change.

## 1.2.3

### Fixed

- **Editing a string or a curtailment group failed with a 500.** Since 1.1.0
  the subentry flows called `async_update_reload_and_abort`, which Home
  Assistant refuses on an entry that has update listeners -- and this
  integration registers one so the options flow takes effect. Subentry edits
  now use `async_update_and_abort` and let that listener reload. Creating a
  subentry still schedules its own reload, because no listener fires for that.

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
