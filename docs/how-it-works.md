# How it works

*[README](../README.md) · [Setup](setup.md) · [Sensors](sensors.md) · [How it works](how-it-works.md) · [Shading](shading.md) · [Accuracy](accuracy.md) · [Curtailment](curtailment.md) · [Savings](economics.md) · [Services](services.md) · [Status](status.md)*

## Compared with plant-level forecasts

Home Assistant is not short of them. Solcast and Forecast.Solar deliver a
number for the plant and deliver it well, and the newer self-learning ones
correct that number against your own meter. If one number for the whole roof
is what you need, take one of those — they are good, and this integration
would only be more work.

The question here is a different one: not *how much arrives today*, but
*which of my strings is losing what, and where*. At plant level that question
has no answer, because the strings are added up before anybody gets to ask it.

## Why per string

Most forecast integrations model one plant with one orientation. If your strings
face different directions, or one is shaded in the morning and another in the
evening, a plant-level model averages those away. It can be 5 % right on the
daily total while being 40 % wrong on every individual string — and you cannot
see it, because the errors cancel.

A learning layer alone does not close that gap. Several forecast integrations
now learn a map of the sky the way this one does, but measured against a single
production total the sums come out the same either way: a shadow on one string
and a thin cloud over the whole site both arrive as one number that fell short.
Telling them apart takes a second string that saw the same minute.

Weather arrives everywhere at once and cancels out between the strings; what is
left on one of them alone is its own obstacle. On the reference plant two
strings of identical orientation and tilt read 0.3 % and 67.7 % loss in the
very same patch of sky — one has a neighbour's house in its morning. How that
is fitted is described in [Shading](shading.md).

This integration keeps them separate, and refuses to let anything blur them:

- **Point each string at its physical measurement channel.** The config flow
  warns when you select a `min_max`, `template`, `group` or similar aggregate
  helper, and blocks it outright when that helper's sources are already used by
  another string. Double-counting one channel while silently dropping another is
  invisible in the plant total and fatal per string.
- **Geometry has a history, not a value.** See [Changing the geometry](setup.md#changing-the-geometry).
- **Curtailment is tracked per group**, because it does not hit every string at
  once.

## Forecast chain

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

### Learning layer

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
