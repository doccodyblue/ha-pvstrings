# Changelog

## 1.15.0

### Fixed

- **The sky map could normalise a shadow away entirely.** Every cell's loss is
  measured against one number per string — what that string reaches where
  nothing is in the way. That reference was the 0.90 quantile over only the
  cells with at least twelve observations.

  On a roof shaded until early afternoon the sun crosses the shaded morning
  sky low and slowly, so the *shaded* cells collect the most observations,
  while the clear afternoon cells are crossed quickly and stay just under the
  threshold. The filter then discarded every bright cell and kept only shaded
  ones. Measured on a live plant: the reference settled at 0.317, so the map
  treated 31 % of physics as a clear view. Every cell came out at or above
  that, clamped, and the map reported a flawless sky over a string producing
  18 % of its forecast all morning. A sibling on the same roof escaped only
  because three of its well-sampled cells happened to be bright ones.

  The reference is now weighted by evidence instead of filtered by it. A thin
  cell still cannot set the standard alone, but a bright one is never thrown
  away. Excluding cells can only ever lower this estimate, and lowering it is
  the ruinous direction.

  Maps are refitted from the raw observations at every startup, so the
  correction applies to everything already collected — no backfill needed to
  recover a shadow that was measured but normalised away.

### Added

- The sky map now reports each cell's own `ratio` — measured over physics,
  before normalisation — and the `reference_ratio` the whole map is measured
  against. Without them a flat map is unreadable: nothing in the way and
  everything equally in the way look identical, and they want opposite fixes.
  This is what made the bug above findable.

## 1.14.1

### Fixed

- The group forecast sensor was named "Remaining forecast <group>" while
  already living on a device named after the group, so it read "Speicher
  Remaining forecast Speicher". Named plainly now; Home Assistant supplies the
  device prefix. Entity ids created by 1.14.0 keep their stutter -- Home
  Assistant never renames an id it has issued -- so rename them by hand if you
  installed that version, before anything depends on them.

## 1.14.0

### Added

- **A forecast per curtailment group.** Every configured group now gets its own
  device with a `Remaining forecast` sensor: how much of what is still to come
  today can reach *that* inverter. Attributes carry today's and tomorrow's
  totals, the hourly series in the usual `forecast` format, and the member
  strings.

  A controller deciding what to do with a surplus needs this and cannot
  reconstruct it. The plant total says nothing about which inverter the energy
  arrives at, and deriving the share from what has already been produced
  describes the wrong half of the day: on a plant whose groups face different
  ways, the share moves with the sun. Measured on a plant with a south-facing
  battery group and an east-facing grid group, a share taken from the morning
  understated the battery group's share of the *remaining* day by 35 points at
  09:00 and was still 11 points low at 18:00 — so the clipping risk read low,
  and the surplus was steered into the house later than it should have been.

  Summed only after each string has been evaluated against its own geometry,
  so one group may hold strings that face different ways or that changed
  orientation on different dates. Strings belonging to no group are in no such
  total, which is why the groups need not add up to the plant; no synthetic
  "ungrouped" total is invented.

  Plants without curtailment groups — the common case — gain no devices, no
  entities and no attributes.

## 1.13.0

### Added

- **Optional charge-controller state per string.** A solar charger that has
  finished bulk charging holds the battery at a voltage instead of tracking
  maximum power — so what it harvests is its own decision, not what the sun
  offered. Nothing commands a limit while this happens, and on Victron the
  state entity is the only place the controller says so. Point the new field
  at it and those intervals are treated as lower bounds, exactly like a
  commanded limit.

  Sharper than inferring it from the battery's state of charge, because it
  also catches the absorption phase, where the battery is not yet full but has
  already stopped taking everything on offer.

  Entirely optional and absent by default. Inverters that expose no such
  entity — most of them — behave exactly as before, and a state word the
  integration does not recognise yields no verdict rather than being read as
  permission to learn from the interval.

## 1.12.0

### Fixed

- **The per-string × daypart layer switches on for the first time.** Its gate
  was set to 25 effective observations, and a bucket cannot hold more than
  about 22: `Effect.update` decays the count before adding new weight, so it
  converges on `1 / ALPHA` and stops. The third layer of the correction model
  therefore accumulated evidence for ever, was filtered back out of the
  diagnostics by the same threshold, and never once reached a forecast — on
  any installation, however long it ran.

  The gate is now a share of what a bucket can actually hold, so the two
  constants cannot drift apart again, and a test asserts the relationship
  rather than the number.

  This changes forecasts: a string that is weak in the morning only — one row
  shaded by a gable at breakfast, say — now gets that correction instead of
  having it smeared across the whole plant.

## 1.11.0

### Fixed

- **A full battery no longer teaches the model that the plant is broken.** On a
  battery-coupled group the battery eventually stops accepting charge, the
  inverter backs off to what the house and the feed-in path can take, and the
  strings follow it down. Nothing commands a limit, so nothing in the data said
  the measurement was held back -- and every such interval was learned at full
  weight as genuine underperformance. On a sunny plant that recurs every
  afternoon at the same sun positions, which is exactly the signature the sky
  map reads as a permanent obstruction: the correction was on its way to
  becoming a phantom shadow with a two-year half-life.

  Such intervals are now marked as lower bounds, which the learning layer
  already treats correctly -- they may push the model up, never down -- and
  which keeps them out of the sky map and out of the uncensored accuracy
  figures entirely.

  Requires `battery_coupled` on the curtailment group and a battery SOC entity
  on the plant. Where the state of charge is unknown nothing is censored:
  guessing there would throw away good observations on no evidence.

  A full battery alone does not censor. The strings may equally have been dim,
  and censoring a dim hour discards a perfectly good observation -- so the
  shortfall against physics has to be real as well. If your BMS reports full
  early, lower `soc_limit_pct` on the group.

## 1.10.0

### Added

- **Day-ahead accuracy.** Every accuracy figure published until now compared an
  hour against the forecast issued minutes before it — a nowcast, and no answer
  at all to the question this integration exists for. `Day-ahead accuracy
  7 days` scores each complete local day against the forecast as it stood at
  18:00 local time the evening before: one coherent model run, and exactly the
  number somebody would have read off the dashboard. `Day-ahead accuracy
  30 days` and `Day-ahead bias 30 days` sit alongside it as diagnostics.
  Expect these to read worse than the existing WMAPE sensors; that gap is the
  honest cost of forecasting a day ahead rather than an hour.
- **Daily bias** (`daily_bias_kwh` in the score attributes). The existing
  `bias` is a mean over *hours*, which cannot be read as "typically half a
  kilowatt-hour too optimistic per day" — the form the question is actually
  asked in. Both are now reported, and the attributes say which is which.
- Nothing is published until three complete days are in, so a single day's
  weather cannot masquerade as an accuracy figure. Until then the sensors read
  `unknown` while `days_scored` shows how far off publishing is.

### Changed

- **`Deviation yesterday` now compares against the evening-before forecast.**
  It summed the *nowcasts* of the previous day, which reads as a comparison
  against something that was announced when it was nothing of the sort. The
  displayed deviation will typically grow, without anything having got worse.
- **Forecast issues are kept for 35 days instead of 14.** Past that horizon
  thinning left only the newest issue per hour — the nowcast — so a day-ahead
  lookup found nothing and dropped the hour from the score without a word. The
  horizon now outlives the widest window scored over.

## 1.9.0

### Added

- **`Rain probability tomorrow` sensor.** The highest hourly chance of rain
  over the day, from the same forecast run that drives the yield prediction,
  with cloud cover and rain volume for today and tomorrow as attributes.

  It exists because a controller deciding how much battery to hold back
  overnight should read the weather rather than infer it. The Node-RED flow
  this integration feeds was classifying tomorrow from a kilowatt-hour figure
  -- eight or more meant sunny -- which is exactly backwards on a clear cold
  winter day: little yield, classified as rain, and the battery holds a
  reserve it does not need.

  `precipitation_probability` is now requested from Open-Meteo and stored per
  hour, and mapped on the Home Assistant weather fallback path too. The
  reanalysis archive shares the request and answers with nulls rather than an
  error, which is correct -- a record of what happened carries no likelihood.

### Changed

- Schema 3: `weather_forecast` gains `rain_probability_pct`. Existing
  databases are altered in place; rows written before the upgrade keep a null,
  because the source was never asked for it.

## 1.8.0

### Added

- **The correction chain, per hour.** The forecast attribute now reports what
  each layer did: `source_bias` on the irradiance, then `physics_kwh`,
  `shading` and `model` on the energy. The three energy factors multiply out
  to the published figure exactly. The irradiance bias is deliberately not one
  of them -- it was applied upstream and is already inside `physics_kwh` --
  and it is named `source_bias` so nothing downstream multiplies it in twice.
- **`Sky map` sensor, per string.** Every fitted cell as
  `(azimuth, elevation, loss, n, season)`, so a card can draw the sky instead
  of tabulating it. A ranked list of the six worst sectors cannot show a gable
  edge or the outline of a tree, and the shape is the entire reason for
  indexing on sun position.

  On its own sensor rather than beside the live shading figure: Home Assistant
  deduplicates attribute blobs by hash, and a static map sitting next to a
  moving sun position would be written to the recorder again every update.

  Seasonally split cells are included and labelled, because those are what the
  forecast looks up once it knows the date -- a map without them would show
  the pooled value while the forecast quietly used a different one.

## 1.7.1

### Fixed

- **The sky map now refits when the evidence moves, not once a day.** The
  daily cadence was introduced to keep a mature map affordable -- in steady
  state the table holds one row per five-minute interval per string across
  the retention window -- but it is badly wrong at the other end of a plant's
  life. A two-day-old map gains half its size in a single morning, and holding
  that back until tomorrow means a whole day of sun corrects nothing. Observed
  on the reference plant: 502 observations in the database against a map still
  built from the 382 it had at breakfast.

  The trigger is proportional growth, per string, which handles both ends
  without a special case: a young map follows a morning immediately because a
  morning is a large fraction of what it knows, a mature one falls back to the
  daily floor. Per string because the maps are per string -- a plant-wide
  total would let one long-established string hold back a new or repaired one.

## 1.7.0

### Added

- **The forecast attribute now also carries `unshaded_kwh`** -- what the same
  hour would have been predicted at with the sky map switched off. Plotted
  against the published forecast it separates two things that used to be one
  indistinguishable gap: the shadow the model has learned about, and the part
  it has not.

  Both ceilings apply to the bare curve too. The chain is exactly linear in
  the shading factor, so the unshaded value is recovered by division rather
  than a second physics pass -- but a capped tracker and the nameplate clip
  are precisely where that linearity stops, and dividing an already capped
  value back out would invent power the module could never make.

## 1.6.0

### Added

- **The log now says whether the integration is working, not just whether it
  crashed.** Until now nothing was written on the happy path, so a clean log
  meant only "nothing raised" -- and two installations sat for days capturing
  or learning nothing with no outward sign at all.

  - One line at startup: strings, groups, irradiance source, whether a
    measured sensor is configured, whether learned correction is on.
  - One line per learn cycle, in plain words: how many hourly rows were
    folded, how many observations were learned, how many were skipped and for
    which reasons, and how much shading, bias and censoring was seen.
  - A warning when the plant captures nothing while the sun is well up, held
    back until it has persisted across several updates so a restart does not
    trigger it.
  - A warning when daylight cycles keep producing no learned observations.
    Deliberately narrow: night hours fold rows and learn nothing entirely
    correctly, and the cycle runs hourly around the clock, so judging on
    folded rows alone would have raised the alarm before breakfast every day.
    A plant with learned correction switched off is never warned about.

  Both warnings fire once when a problem sets in, and re-arm only after it
  clears.

## 1.5.4

### Changed

- **`Shading now` reports the loss, not the surviving fraction.** It read
  100 % when nothing was in the way, which is exactly backwards from what the
  name says -- and it was misread that way immediately, by the person who
  asked for the sensor. 0 % is now a clear view and 100 % a panel in full
  shadow. A plant that has not learned any sky yet reads 0 % rather than
  looking permanently and totally shaded.
- The shaded sectors in the attributes report `shading_pct` on the same
  convention, worst first.

## 1.5.3

### Fixed

- **Learning was dead on every installation with an irradiance sensor.** When
  a measured GHI replaces the forecast one, the forecast's direct and diffuse
  components no longer belong to it and are blanked, leaving the decomposition
  to derive them. It never ran. `components_plausible` is a closure test, and
  a closure test on a missing value cannot fail, so it answered "nothing
  wrong" -- which was read as "usable". The blanked components then reached
  `fillna(0.0)` and became a hard zero: a plant standing in 640 W/m2 modelled
  with no beam and no diffuse light, only ground reflection.

  The physics came out around a hundredth of the truth, so every
  measured-versus-physics ratio blew past the sanity bound and the log-ratio
  model, the shading map and the curtailment detection all silently stopped
  learning. On the reference plant this showed as one usable observation an
  hour out of five, no shading observations at all for three of five strings,
  and no curtailment ever detected. After the fix: five of five, and shading
  observations from every string.

  The bitter part is who it hit: only installations that had gone to the
  trouble of fitting an irradiance sensor, because that is the path that
  blanks the components in the first place.

### Added

- **Recent hourly rows in diagnostics.** Coverage, quality and value kind per
  hour per string for the last day. Every question about why the model is not
  learning ends there, and without them the answer has to be inferred from a
  counter.

## 1.5.2

### Added

- **The learning model says why it declined an observation.** "Not used"
  covered five different situations -- no weight, no physics, no production,
  an absurd ratio, and a censored hour the physics already explains. On a
  plant where four strings in five are dropped every hour, the difference
  between them is the whole diagnosis.

## 1.5.1

### Added

- **Skipped observations now say why.** The learn cycle reported a bare count
  of skipped observations, which is not an observation but a shrug: night, a
  missing physics row, zero physics in broad daylight, thin coverage and an
  out-of-range ratio are four quite different problems behind one number, and
  a plant can sit at zero learned observations for days with nothing to point
  at. `skipped_because` breaks the count down by reason.

## 1.5.0

### Added

- **`Shading now` sensor, per string.** How much of the sun's *current*
  position the string can actually see, as a percentage: 100 % is a clear
  view. A static table of sky cells says very little on a dashboard, because
  the whole point of the map is that it varies with the sun -- one live number
  per string is what tells you the tree is in the way, and it plots across the
  day to draw the shadow's edge. Attributes carry the observation count, how
  many sky cells that string has covered, and how many of them needed a
  seasonal split.
- **The sky map in diagnostics.** `model_observations` now reports the fitted
  map alongside the log-ratio and irradiance-bias tables, so all three learned
  layers can be read in one place.

## 1.4.2

A second review pass over the 1.4.1 fixes found two regressions that the
fixes themselves had introduced. Both are corrected here, each with a test
verified to fail against the broken code.

### Fixed

- **The thinning deleted the entire backfill.** Two fixes that were each
  right destroyed the feature between them: backfilled rows are stamped one
  second off the five-minute grid so they cannot overwrite real measurements,
  and old rows are thinned to a quarter to keep the refit affordable. Because
  an hour holds twelve intervals, every backfilled row lands on the same
  residue of `(ts / 300) % 4` -- never zero -- so the thinning removed all of
  them rather than three quarters. A 540-day backfill lost everything past
  four months on its first night, taking the winter cells it was run for with
  it. Thinning now applies only to the dense five-minute grid; hourly
  backfilled rows have nothing to thin.
- **"Apply learned correction" did not cover the shading map.** The log-ratio
  layer honoured `learning_enabled`; the shading map checked only the internal
  argument, which the normal forecast always passes as true. A plant with the
  switch turned off kept being multiplied down by a map it had been told to
  ignore -- and, with learning off, no longer collected the observations that
  would have justified it.

## 1.4.1

An independent multi-agent review of 1.4.0 found ten defects, two of which
made the headline features of that release inert. All ten are fixed here, and
the two that shipped dead now have end-to-end regression tests that were
verified to fail against the broken code.

### Fixed

- **The shading factor never reached the physics.** `_interval_power`
  computed a solar position for the lookup and then called `physics.run`
  without passing the factor, so the two supposedly separate passes in the
  learn cycle were byte-identical unshaded physics. The log-ratio model
  absorbed each shadow into its per-string effect while the forecast path
  subtracted it a second time, and the map was double-counted on every shaded
  string.
- **The irradiance plausibility guard never ran.** Inside `learn()` the check
  read the hourly fold that the same cycle writes one line later, found it
  empty, and memoised that answer for the whole window. It now reads the
  five-minute rows the collector writes independently, so no ordering can
  starve it.
- **The backfill skipped the unit conversion the collector applies.** An
  inverter publishing kilowatts reconstructed ratios a thousand times too
  small, which nothing downstream recognised as a unit mismatch rather than a
  very deep shadow. Backfilled ratios below two percent are now rejected and
  logged as well.
- **Backfilled observations overwrote real ones.** They were stamped at the
  hour midpoint, which is a valid five-minute interval start, so the upsert
  replaced one genuine measurement in twelve on the first run.
- **Thin cells defined "unshaded".** The reference level was taken over shrunk
  values, so the emptiest corners of the sky set the standard and an
  unshaded string with optimistic physics came out shaded everywhere.
- **`reset_learning` left the sky map in place**, so a reset removed the
  per-string effects that were offsetting it and left the forecast worse than
  before. It now clears the observations too -- also the only way back from a
  bad backfill.
- **The backfill offered four years of history against a two-year retention**,
  so half of a long run vanished at the next nightly purge.
- **A sensor gap could convict a healthy hour.** The ceiling was a mean over
  the intervals the sensor reported, compared against energy over the whole
  hour. Hours with less than 80 % irradiance coverage are now left unjudged.
- **Twilight convicted itself.** Sensors that round to 0 W/m2 at dawn and dusk
  produced a zero ceiling against real production. Production below a
  nameplate-scaled floor is no longer judged.
- **The sky map was refitted every daylight hour** from the entire
  observation table, which reaches six figures in steady state. Refits are now
  daily, the fitter no longer builds a second full copy of every observation,
  and observations past a season are thinned to a quarter of their density.

## 1.4.0

### Added

- **Per-string shading correction.** The sky map that the collector has been
  filling since 1.0 is now actually applied: a grid over sun azimuth and
  elevation, learned per string, multiplied onto the effective irradiance.
  A chimney shadow sits at a fixed place in the sky, so the map is indexed by
  sun position rather than by clock time and stays correct across the seasons.
  Cells nobody has observed yet correct nothing at all.
- **Deciduous shading.** The sun reaches each point in the sky twice a year,
  once while the days lengthen and once while they shorten. For a wall those
  visits are identical; for a tree they are not. Each cell splits in two
  whenever -- and only whenever -- its own observations say the halves
  disagree, so a site shaded by buildings keeps its full weight of evidence
  and a site shaded by a tree gets the distinction it needs, with nobody
  having to describe their garden to a config flow.
- **Forgetting.** Observations decay with a two-year half-life, measured
  against the newest data rather than the wall clock. Trees grow, sheds go up
  and hedges get cut; the map has to be able to change its mind.
- **`pvstrings.backfill_shading` service.** Reconstructs shading observations
  from Home Assistant's long-term statistics and a historical irradiance
  archive, so the correction can start from months of real data instead of
  waiting a full turn of the seasons. On a five-string plant with sixteen
  months of recorder history this produced 14 910 observations covering 60 to
  117 sky cells per string.

### Fixed

- **The irradiance ceiling assumed nobody lives where it snows.** Ground
  reflection was bounded at an albedo of 0.3; fresh snow reaches 0.9, and a
  steep plane over snow collects a fifth of the horizontal irradiance again
  from the ground alone -- enough to have thrown away good winter hours as
  impossible. Now bounded at the physical worst case.
- **The backfill's minimum-power threshold was absolute.** 25 W means
  something quite different to a 300 Wp balcony panel and to a 30 kWp roof;
  it now scales with nameplate.
- **A mis-reading irradiance sensor could no longer be detected.** A measured
  GHI is used as truth in three places at once -- it drives the physics that
  actuals are compared against, it is the yardstick for the forecast bias, and
  it is the denominator of every shading observation -- so a sensor that reads
  low for part of the day corrupted all three while each stayed
  self-consistent. Hours where the array produced more than the measured
  irradiance physically allows are now dropped.
- **Irradiance bias observations are weighted by irradiance.** A 20 W/m2 dawn
  hour previously counted as heavily as a 600 W/m2 midday one, letting the
  least consequential part of the day dominate a correction applied to all
  of it.

### Measured

Out-of-sample on a five-string plant, trained on history to 2026-06-01 and
scored on the hours after it: WMAPE 41.3 % -> 31.8 %, bias +21.6 % -> +0.4 %.

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
