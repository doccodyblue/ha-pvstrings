# PV Strings

> ## ⚠️ Beta — largely unproven in the field
>
> This is version 1.1.0 of an integration that has been running on **exactly
> one installation, for one day**. Treat every number it produces as
> provisional.
>
> **What that means concretely:**
>
> - The learning layer needs weeks of data before its corrections mean
>   anything. Until then you are looking at pure physics plus noise, and the
>   accuracy sensors will show figures based on a handful of hours.
> - Only Home Assistant **2026.8** has actually been run. The 2025.9 minimum
>   was established by checking that the required APIs exist in that release,
>   not by running it.
> - The database schema may still change. There is a migration mechanism, but
>   no upgrade path has been exercised yet, and a future version may ask you
>   to start over.
> - Expect breaking changes between minor versions until 2.0.
>
> **What is tested:** 249 automated tests cover the physics chain, the learning
> rules, censoring, storage and the config-flow schemas — including the parts
> that only fail against a real Home Assistant. The core logic is exercised;
> the integration as a whole is not battle-hardened.
>
> Bug reports are very welcome. Please include the diagnostics download.


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
| Forecast today / remaining / tomorrow | hourly detail in the `forecast` attribute |
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

All energy sensors carry proper `device_class` and `state_class`, so they work
in the Energy dashboard and in long-term statistics.

---

## Installation

### HACS (recommended)

Add this repository as a custom repository of type *Integration*, install, then
restart Home Assistant.

### Manual

Copy `custom_components/pvstrings/` into your Home Assistant `config/custom_components/`
directory and restart.

### Requirements

**Home Assistant 2025.9 or newer.** That floor is not a guess -- it is where
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
coordinates. The default irradiance source is Open-Meteo — free, global, no API
key, and it exposes the actual GHI/DNI/DHI components.

**2. Add a curtailment group** (optional, one per inverter that can be limited).
A group carries the limit entity, the inverter nameplate, and optionally the
battery SOC. Skip this entirely if nothing throttles your inverters.

**3. Add one subentry per string.** Name, power sensor, azimuth, tilt, kWp, and
which curtailment group it belongs to. Each string becomes its own device.

**4. Optional, in the options flow:** grid power (enables export-aware savings),
house load, battery, and any local weather sensors. A pyranometer or illuminance
sensor lets the bias model learn against measured irradiance rather than the
forecast's own analysis.

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
                      ( + string_daypart[string, daypart]  once the bucket is populated )
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

Three tariff models, and all three are always computed side by side:

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

## Not in this version

- No `usable_kwh`. Reporting how much of the potential you can actually *use*
  needs a load forecast and a time-resolved battery simulation. Both are their
  own subproject; guessing would be worse than not answering. Only
  `potential_kwh` is published. The economics run on measured values and are
  unaffected.
- No automatic shading **correction**. Raw observations (azimuth, elevation,
  ratio) are collected from uncensored intervals so the analysis can be done
  later on a year of data. They are stored unrasterised on purpose: a fixed grid
  built from thin data is a lossy commitment.
- No neural networks or tree ensembles, no champion/challenger, no multi-source
  blending, no snow model.

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
