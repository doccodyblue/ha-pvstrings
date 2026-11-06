# Setup

*[README](../README.md) · [Setup](setup.md) · [Sensors](sensors.md) · [How it works](how-it-works.md) · [Shading](shading.md) · [Accuracy](accuracy.md) · [Curtailment](curtailment.md) · [Savings](economics.md) · [Services](services.md) · [Status](status.md)*

## Requirements

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

## Configuration

**1. Add the integration.** Location is prefilled from your Home Assistant
coordinates. The default irradiance source is [Open-Meteo](https://open-meteo.com/)
— free, global, no API key, and it exposes the actual GHI/DNI/DHI components.
Your coordinates go to Open-Meteo with each request; the [weather-entity source](#offline-weather-source)
keeps everything on your own machine.

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

**Temperature and wind refine the cell-temperature model.** The physics reads
module temperature off air temperature and wind speed (Sandia model, per mount
type), and a string on a still 35 °C afternoon runs several percent below the
same string in a 20 °C breeze. The forecast always supplies both; where a
local temperature or wind sensor is configured, its reading replaces the
forecast value for every interval it covered when past hours are
reconstructed for learning. Without the sensors that difference is booked as
weather-class weakness; with them it stays in the physics. Units are read
from the entity, so °F and km/h are fine. Readings outside −40…60 °C or
0…60 m/s are treated as a sensor fault and fall back to the forecast.

What heat costs is reported rather than hidden: every forecast hour carries a
`thermal` factor in its chain attributes (the physics over the same physics at
25 °C cells, next to `source_bias`, `shading` and `model`) with the
`cell_temp_c`, `air_temp_c` and `wind_ms` behind it, and each string has a
diagnostic *Cell temperature* sensor whose `heat_loss_today_kwh` sums the
day. The `Irradiance forecast` sensor's `station_air_share_today` says how
much of today's daylight the temperature and wind sensors actually covered.

### Offline weather source

Switch the irradiance source to *Home Assistant weather entity*. Cloud cover is
converted to irradiance with an empirical relation (Kasten-Czeplak) — noticeably
worse than real components, but it works. The diagnostics say which source was
used.

## Changing the geometry

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

A correction also carries the string's shading observations over to the new
plane, so the sky map does not have to be relearned. For a correction made
before v1.26.1, `pvstrings.rebase_shading` does the same after the fact.
