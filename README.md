![PV Strings](custom_components/pvstrings/brand/icon.png)

# PV Strings

*Home Assistant 2025.9 or newer · MIT*

A Home Assistant integration that forecasts and evaluates PV yield **per string**,
not per plant.

Physics first: `pvlib` turns an irradiance forecast into an expected potential
for each string, using that string's own azimuth, tilt and nameplate. It works
on day one, with no training. A learning layer then corrects only what the
physics still gets wrong.

Built for small installations — balcony plants, garden sheds, a couple of
inverters, adjustable mounts — where the per-string detail is the whole point
and a plant total hides everything interesting.

---

## Why per string

Most forecast integrations model one plant with one orientation. If your strings
face different directions, or one is shaded in the morning and another in the
evening, a plant-level model averages those away. It can be 5 % right on the
daily total while being 40 % wrong on every individual string — and you cannot
see it, because the errors cancel.

This integration keeps them separate, and refuses to let anything blur them:

- **Point each string at its physical measurement channel.** The config flow
  warns when you select a `min_max`, `template`, `group` or similar aggregate
  helper, and blocks it outright when that helper's sources are already used by
  another string. Double-counting one channel while silently dropping another is
  invisible in the plant total and fatal per string.
- **Geometry has a history, not a value.** See below.
- **Curtailment is tracked per group**, because it does not hit every string at
  once.

---

## What you get

Per plant:

| Sensor | Notes |
|---|---|
| Forecast today / remaining / tomorrow | hourly detail in the `forecast` attribute; *remaining* is a **subset of today**, see below |
| Forecast next hour, peak hour today | |
| Produced today | measured, from the integration's own 5-minute data |
| Deviation yesterday | what the evening-before forecast said, vs. actual |
| Day-ahead accuracy 7 d | how good "tomorrow" actually is; see *Metrics* |
| Day-ahead accuracy 30 d, day-ahead bias 30 d | diagnostic |
| WMAPE 7 d / 30 d, Bias 7 d | diagnostic; nowcast quality, see *Metrics* |
| Savings today / month / total | measured energy, not forecast |
| Amortisation | progress, months remaining, target date |
| Model observations, collector coverage | diagnostic |

Per string: forecast today / remaining / tomorrow, potential this hour, produced
today.

Per curtailment group, on its own device: a **Remaining forecast** — how much of
what is still to come today can reach *that* inverter, with today's and
tomorrow's totals, the hourly series and the member strings as attributes. It is
what a controller needs to decide whether a surplus is about to be thrown away,
and it cannot be derived from the plant total. Plants without groups get none of
this.

### Remaining is part of today, not a second summand

Two users on two installations independently added *forecast today* and
*remaining today* expecting the day's total. It is not: `today = elapsed +
remaining`, and every remaining sensor now says so in its attributes
(`forecast_today_kwh`, `forecast_elapsed_kwh`).

The hour that has already started is **split at the minute**, on the
five-minute series the forecast was built from — not pro-rated linearly, which
near sunrise and sunset is off by a factor rather than a rounding. The
attribute `split_source` names which of the two happened: `fine` for the real
split, `hourly_stale` when a refresh was missed and the running hour had to be
counted whole.

### Measured sensors carry a `state_class`, forecasts do not

The measured ones — produced today, the savings figures — do, so they work in
the Energy dashboard and in long-term statistics. The forecast sensors
deliberately do not: a prediction that rises and falls through the day is not a
meter reading, and letting the recorder accumulate statistics from it produces
a number with no meaning. Home Assistant's own solar-forecast integration does
the same.

### Reading it back out

Everything above is an ordinary entity with ordinary attributes — nothing is
locked inside a custom card. The hourly series sits in the `forecast` attribute,
templates and automations read it like any other sensor, and the per-group
*Remaining forecast* exists precisely so a controller can ask how much is still
to come behind one inverter before it decides to throttle or divert.

That is the intended use: this integration answers *what the plant could
produce*, and leaves *what to do about it* to your automations.

If you would rather see it than query it,
**[PV Strings Dashboard](https://github.com/doccodyblue/ha-pvstrings-dash)** is a
separate HACS repository — cards for the sky map, the forecast, the conversion
path and the per-hour correction chain, plus a strategy that builds a whole
dashboard from your entity registry without a card editor. It is optional, and
it draws nothing the integration does not already publish.

---

## Installation

### HACS (recommended)

Search for **PV Strings** in HACS, install, then restart Home Assistant.

Not there yet? The repository is awaiting inclusion in the HACS default store,
which takes a while. Until then add it by hand: HACS → ⋮ → *Custom
repositories* → `https://github.com/doccodyblue/ha-pvstrings`, type
*Integration*.

### Manual

Copy `custom_components/pvstrings/` into your Home Assistant `config/custom_components/`
directory and restart.

### Requirements

**Home Assistant 2025.9 or newer.** That floor is not a guess — it is where
`ConfigSubentryFlow.async_update_reload_and_abort` appears, which the string
and group editors rely on. Earlier releases fail progressively: 2025.2 has no
subentry API at all, 2025.3 gains one without `_get_entry`, and 2025.6 still
lacks the reloading variant. Developed and run against 2026.8.

`pvlib` is installed automatically. It pulls in `numpy`, `pandas`, `scipy` and
`h5py`; on Home Assistant OS these come from the prebuilt wheel index. The
integration deliberately does not pin `numpy` or `pandas` itself, so it cannot
fight with the versions Home Assistant already ships.

Currency follows `hass.config.currency`, so the savings sensors are labelled
correctly outside the euro zone.

---

## Setup

**1. Add the integration.** Location is prefilled from your Home Assistant
coordinates. The default irradiance source is [Open-Meteo](https://open-meteo.com/)
— free, global, no API key, and it exposes the actual GHI/DNI/DHI components.
Your coordinates go to Open-Meteo with each request; the weather-entity source
below keeps everything on your own machine.

**2. Add a curtailment group** (optional, one per inverter that can be limited).
A group carries the limit entity, the inverter nameplate, and optionally the
battery SOC. Skip this entirely if nothing throttles your inverters.

**3. Add one subentry per string.** Name, power sensor, azimuth, tilt, kWp, and
which curtailment group it belongs to. Each string becomes its own device.

**4. Optional, in the options flow:** grid power (enables export-aware savings),
house load, battery, and any local weather sensors.

**The irradiance sensor is the one worth having.** Without it the source-bias
layer — the single largest correction in the chain — has nothing to check the
forecast against except the forecast's own shortest-horizon run. It can then
learn how the forecast *decays with lead time*, but not whether it is wrong in
the first place. With a sensor it learns both. The `Irradiance forecast` entity
reports which of the two is in use as `truth_source`, so you can tell at a
glance.

**Mount it horizontally.** The reading is used as global horizontal irradiance:
the physics layer decomposes it into direct and diffuse and then transposes
that onto each string's own plane. A sensor lying in the plane of the modules
has already been tilted once, and would be tilted a second time — an error that
travels with the sun rather than scaling out, so no learned correction removes
it. A weather station's radiation sensor is horizontal by construction and is
the easy right answer.

It does not have to be a pyranometer. A weather station's lux-derived figure
recovers most of the benefit: a constant scale error cancels out between this
layer and the per-string one, and what remains is the spectral drift with cloud
cover.

### No internet access?

Switch the irradiance source to *Home Assistant weather entity*. Cloud cover is
converted to irradiance with an empirical relation (Kasten-Czeplak) — noticeably
worse than real components, but it works. The diagnostics say which source was
used.

---

## Geometry is a validity history

Adjustable mounts are normal on small installations. If you steepen a panel for
winter and simply overwrite the tilt, every past hour is retroactively evaluated
against a geometry that was not installed at the time.

That is not a constant offset. It **travels with the sun**:

| | cos(AOI) at 60° | at 70° | error |
|---|---|---|---|
| August, midday (sun ~55°) | 0.91 | 0.82 | **~11 %** |
| December, midday (sun ~13°) | 0.96 | 0.99 | ~3 % |

A learning layer looking at that sees a seasonal pattern and books it as a
weather-class or shading effect — a silent, drifting error of exactly the kind
this project exists to remove.

So when you change azimuth, tilt or kWp, the integration asks **when it started**:

- *From now on* — the usual case
- *From a specific date* — you moved it last month and are catching up
- *It was wrong from the start* — a typo; this corrects the latest period
  instead of appending one

Past data keeps being computed against the geometry that was actually up. The
recorded periods are shown in the edit dialog and in the diagnostics download.
There is also a `pvstrings.add_geometry` service for automations.

---

## How the forecast is built

```
Open-Meteo GHI/DNI/DHI (hourly)
  → GHI bias correction per (local hour × forecast horizon)
  → downscaled to 5 minutes, holding the CLEAR-SKY INDEX constant
  → solar position at each interval MIDPOINT
  → component plausibility check: GHI ≈ DHI + DNI·cos(z)
      fails → derive components from GHI (Erbs), fall back to Hay-Davies
  → transposition (Perez-Driesse), IAM, cell temperature, pvwatts_dc
  → learned log-ratio correction
  → potential_kwh per string per hour
```

Two details that are easy to get wrong:

- **Interval midpoint, not interval start.** Evaluating solar position at the
  start of a five-minute window produces a systematic transposition error that
  grows towards sunrise and sunset.
- **Constant clear-sky index, not constant GHI.** Spreading an hourly irradiance
  value flat across the hour is badly wrong near sunrise, where the clear-sky
  curve moves by a factor of several within that hour.

### The learning layer

```
log(actual / physics) = plant_effect[weather_class × daypart]   (12 shared effects)
                      + string_offset[string_id]                (strongly regularised)
                      ( + string_daypart[string, daypart]  shrunk to neutral while thin )
```

Forecast errors act plant-wide; mounting, nameplate and shading errors act per
string. Splitting them that way shares information between strings instead of
estimating a dozen thin buckets independently. Dayparts are relative to **solar
noon**, not the clock.

Buckets use a rolling weighted mean whose effective count decays with the same
half-life as the mean itself, so shrinkage and averaging count one history
rather than two.

**Curtailed hours may only ever push the model up.** An inverter sitting at its
limit tells you the true potential was *at least* that high — never that it was
that low. Without this hinge, a summer of clipping teaches the model that your
strings are weak.

**GHI bias** is learned per `(source, local hour, horizon bucket)`. A +1 h and a
+48 h forecast do not share a bias. This is expected to be the single largest
lever: the physics chain is deterministic, the irradiance input is not.

---

## Output paths: DC, AC, or battery charge

The forecast core is DC — what the panels deliver. Optionally, each
curtailment group can declare an **output path** that converts its share:

- **`direct`** (DC → inverter → grid/house): forecast of AC energy behind
  the inverter, through a load-dependent datasheet efficiency curve, with
  optional clipping at the inverter's rated AC power. The value is
  *hardware potential*: it is never capped at commanded or legal feed-in
  limits — a plant limited to 800 W by regulation but built bigger will
  see more here than it may feed in.
- **`storage`** (DC → external MPPT → battery): forecast of energy landing
  in the battery. It deliberately ends at the battery terminal — when that
  energy leaves the battery again is a control decision, not a forecast.
  AC and battery-charge forecasts are different quantities and must not be
  summed.

With no path configured (the default) nothing changes: no new entities,
DC-only, identical to every release before v1.20.

---

## Curtailment

A **commanded limit is not curtailment.** At a 1796 W limit with 600 W
available, the measurement is exact. It only becomes a lower bound when the
inverter actually runs into the wall:

```python
binding = measured_w >= limit_w * 0.97 and physics_potential_w > limit_w * 1.05
```

The collector cannot decide this — it knows the limit but not the potential — so
the flag stays `NULL` until physics has run. `NULL` (unevaluated) and `0`
(evaluated, not binding) stay distinguishable in the database on purpose.

When a string is censored, the integration tries to reconstruct its potential
from a demonstrably free peer string, under strict gating: sun above 12°, both
strings meaningfully loaded, neither shaded, and peers agreeing with each other.
The result is a weak pseudo-label, weighted 0.25–0.5, never a measurement.

**When every group is curtailed at once** — summer midday, battery full, load
covered — there is no defensible point value at all. The forecast keeps running
and the GHI bias keeps learning, but the string potential cannot be evidenced.
That is a limit of the method, not a bug, and it is reported as such rather than
papered over.

---

## Metrics

"78.6 % accuracy" is meaningless without a definition. This integration reports:

- **WMAPE** over daily sums: `Σ|forecast − actual| / Σ actual`
- **nMAE** hourly, normalised to installed kWp
- **Bias** — mean signed error, so over- and under-forecasting stay visible
- **Daily bias** — the same, but per day, which is the unit the question is
  asked in: "typically half a kilowatt-hour too optimistic"

Mind the two granularities: WMAPE and daily bias describe **days**; nMAE and
bias are means over single **hours**.

Each is reported **twice**:

1. **Uncensored hours only** — the true potential was measurable. This is model
   quality, and the only figure comparable with other forecast services.
2. **All hours including curtailed** — everyday usefulness.

Scoring never uses hindsight: a forecast issued during the hour it predicts is
not a forecast. The default compares against the last run issued before the hour
started.

That default is a **nowcast**, though, and flatters the model when the question
is "how much will tomorrow bring". The day-ahead sensors therefore score each
local day against the forecast as it stood at **18:00 local time the evening
before** — one coherent model run, and exactly the numbers somebody would have
read off the dashboard. Only complete days count, and nothing is published
until three of them are in, so a single day's weather cannot masquerade as an
accuracy figure. Expect the day-ahead number to sit well above the nowcast one:
that gap is the honest cost of forecasting a day ahead.

### Whose fault was it — the weather service or us?

Any forecast error has two possible culprits: the irradiance the forecast was
handed, and what this integration made of it. The published numbers mix them,
which makes them nearly useless as a development signal — a good week and a bad
week differ mostly by weather.

With an irradiance sensor, the split is measured. Every closed hour is run
through the chain a second time — same physics, same sky map, same learned
correction — but fed the irradiance the sensor **measured** instead of the one
that was forecast. The distance from that to reality is ours; the distance from
the published forecast to it is the source's:

| Attribute of *chain error 7 days* | Answers |
|---|---|
| the sensor's own state | what the chain gets wrong when the irradiance is known |
| `wmape_source_7d` | how far the irradiance forecast alone moved the answer |
| `wmape_end_to_end_7d` | both together, on the same hours |

The two parts are absolute errors and deliberately do **not** add up to the
whole: an over- and an under-shoot cancel in the total and must not cancel
here. The chain figure also flatters itself slightly, because the learned
correction inside it was fitted on those very hours — it is a regression signal
for development, not a claim of accuracy on unseen days.

**Without an irradiance sensor** nothing here changes and nothing breaks: no
counterfactual is computed, no extra work is done, and the sensor states plainly
that no sensor is configured rather than showing an empty tile. Everything else
in the integration works exactly as before.

---

## Data collection

```
state_changed events   → recorded as they arrive
30 s watchdog          → snapshot, so a silent entity still yields support points
5-minute aggregates    → persisted, the primary source
hourly values          → derived from those, never measured separately
```

Raw seconds are never stored. Five-minute resolution is deliberate: an hour can
be twenty minutes free and forty minutes curtailed, and on hourly means that is
no longer separable.

**`unavailable` is not zero.** An inverter dropping below its start-up voltage at
dawn flickers between `0.0` and `unavailable`. With the sun below 3° that is
recorded as `night` — a genuine, learnable zero. With the sun up it is `missing`,
and it is excluded from learning. Writing `float(0)` there would turn a midday
dropout into a learned null the model could never recover from.

Every interval carries an honest `coverage`, and quality follows it:

| Coverage | Quality | Learning |
|---|---|---|
| ≥ 0.95 | `exact` | full weight |
| 0.80–0.95 | `partial` | weight = coverage |
| below, sun up | `missing` | excluded |
| sun down | `night` | value 0 |

---

## Economics

Runs on measured data only, so it is valid before the learning layer is warm.

### What gets valued

Not DC production — **delivered energy**. The strings are measured on their DC
side, but what displaces a purchase is what comes out of the inverter, or back
out of the battery. Each group's measured DC energy is therefore multiplied by
a conversion factor before any price touches it, and the `savings_total`
attributes name what that factor rests on:

| Basis | Where the factor comes from |
|---|---|
| `measured` | the group's own AC sensor — the same pairs the efficiency curve is fitted on, read as one load-weighted ratio. Needs ~200 clean intervals, roughly two days of daylight |
| `curve` | the inverter curve, averaged over the load range this group actually runs at. Clipping is deliberately left out: an inverter that clips pulls its own DC input down with it, and the measured DC energy has already lost that |
| `configured` | a battery path: MPPT × charge × discharge efficiency. All three are configured numbers — battery power is a net flow after the house load, not a two-port, so no side of it is measurable the way an inverter's is. An estimate, and labelled as one |
| `dc` | no output path, or nothing to convert with. Counted on its DC side, exactly as before |

`dc_kwh_total` sits next to `kwh_total` so the two are comparable, and
`delivery.by_basis_kwh` shows how much of the total rests on a measurement
rather than an assumption. An implausible factor — a mis-scaled AC sensor
reading above 1.0, say — is refused rather than applied, and the next rung
down is used instead.

### Tariff models

Three, and all three are always computed side by side:

- `net_metering` — the meter physically runs backwards, so every exported kWh
  displaces an imported one. **Temporary by construction.**
- `self_consumption` — self-used at the retail price, exported at the feed-in
  tariff
- `feed_in` — everything at the feed-in tariff

The scenario comparison in the `savings_total` attributes answers *"what will the
meter swap cost me?"* **before** it happens. For a plant exporting most of its
production under net metering, the honest answer is usually "about two thirds of
the savings".

Annual figures are extrapolated using the site's **own clear-sky seasonality**,
derived from your strings' geometry — not `savings_so_far / days × 365`. Measured
from spring, that linear form runs straight over the yield peak and overstates
the year badly.

Amortisation runs on the same delivered energy, so its target date moves out by
however much your conversion losses are — typically 4–6 % for a direct path,
around 10 % through a battery.

---

## Services

| Service | Purpose |
|---|---|
| `pvstrings.recalculate` | Fetch fresh weather and rebuild the forecast |
| `pvstrings.add_geometry` | Record a new mounting geometry period |
| `pvstrings.reset_learning` | Drop all learned corrections, keep measurements |
| `pvstrings.purge` | Delete raw 5-minute data past the retention period |

---

## Storage

One SQLite database per config entry, at `config/pvstrings/<entry_id>.db`,
separate from the Home Assistant recorder. All timestamps are Unix epoch UTC —
DST creates duplicate and missing local hours, which makes local time unusable
as a primary key.

Raw five-minute data is purged after the retention period (default 3 years).
Hourly aggregates, geometry history and model state are kept indefinitely; they
are tiny and they are the memory of the system.

---

## Shading, and what the map can and cannot see

The sky is divided into cells of ten degrees of azimuth by five of elevation,
and each string learns its own map of them: for every clean five-minute
interval, measured over physics at that sun position. The forecast is corrected
with it. Indexed on the sun rather than the clock, because a chimney's shadow
sits at a fixed place in the sky while the time it arrives drifts by an hour
twice a year.

What it will not do:

- **Invent sky it has not seen.** In August the sun never reaches the winter
  cells, so those correct nothing. A complete map takes a full turn of the
  seasons; `pvstrings.backfill_shading` reconstructs what it can from Home
  Assistant's own history.
- **Carry the string's overall level.** The map is normalised against parity,
  so it reports *shape* — how a string varies across the sky — and leaves the
  level to the per-string log-ratio layer. The cost is real and known: on a
  string whose physics runs low everywhere, a shadow that does not push a cell
  below parity stays invisible.
- **Learn from an interval it should not.** Curtailed intervals, a full
  battery, a charge controller holding a voltage — all are excluded, because a
  throttled afternoon recurs at the same sun positions as a shadow does and
  would otherwise be learned as one.

Each cell reports its own `ratio` and the map its `reference_ratio`, because a
map showing no loss anywhere is unreadable without them: nothing in the way and
everything equally in the way draw the same picture, and they want opposite
repairs.

---

## Not in this version

- No `usable_kwh`. Reporting how much of the potential you can actually *use*
  needs a load forecast and a time-resolved battery simulation. Both are their
  own subproject; guessing would be worse than not answering. Only
  `potential_kwh` is published. The economics run on measured values and are
  unaffected.
- No neural networks or tree ensembles, no champion/challenger, no multi-source
  blending, no snow model.

---

## Where it stands

Running on two installations since 11 August 2026, and developed against Home
Assistant 2026.8. The 2025.9 floor was established by checking that the required
APIs exist there, not by running it. Breaking changes remain possible between
minor versions before 2.0; the schema migrates in place, and the v2 → v3 upgrade
has been exercised against a real database.

778 automated tests cover the physics chain, the learning rules, censoring, the
sky map, storage and the config-flow schemas — including the parts that only
fail against a real Home Assistant.

The model does need time, and says so rather than guessing: no accuracy figure is
published for the first three days, because one day of history makes a confident
percentage out of one day's weather. The sky map learns only the sky the sun has
actually crossed, so a full year is what it takes to be complete — on a fresh
install it corrects nothing, which is the intended behaviour and not a fault.

Three bugs the field found and the test suite did not, all since fixed: a
correction layer whose gate sat above the value it could ever reach, so it never
switched on; a sky map whose reference settled inside a shadow and reported a
flawless sky over a roof that was dark all morning; and the same map, once fixed,
putting phantom loss on a panel with a clear view of the whole sky. All three
looked exactly like "not enough data yet" from the outside — which is why the
diagnostics download carries the model internals. If a number here looks wrong to
you, it may well be: open an issue and attach it.

---

## Data sources

Irradiance and weather come from **[Open-Meteo](https://open-meteo.com/)**, whose
data is published under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
Every entity carries `Weather data by Open-Meteo.com (CC BY 4.0)` as its
attribution while that source is selected. Open-Meteo's free tier covers
"personal home automation purposes" explicitly, and the integration asks for one
forecast every 30 minutes — roughly 48 calls a day against a limit of 10,000.

**If you run your plant commercially**, Open-Meteo counts that as commercial use
and expects an API key. That is between you and them; nothing in this integration
changes on your side either way.

The physics runs on **[pvlib](https://pvlib-python.readthedocs.io/)**
(BSD-3-Clause), installed by Home Assistant rather than shipped here.

---

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest
```

`custom_components/pvstrings/core/` imports no Home Assistant code at all. It is
plain Python: physics, learning, storage and scoring can be run, tested and
benchmarked offline against a copy of the database. The Home Assistant layer
above it does entities, config flow and scheduling, and pushes every blocking
call into the executor.

---

## Licence

MIT
