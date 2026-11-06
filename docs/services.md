# Services and storage

*[README](../README.md) · [Setup](setup.md) · [Sensors](sensors.md) · [How it works](how-it-works.md) · [Shading](shading.md) · [Accuracy](accuracy.md) · [Curtailment](curtailment.md) · [Savings](economics.md) · [Services](services.md) · [Status](status.md)*

## Services

| Service | Purpose |
|---|---|
| `pvstrings.recalculate` | Fetch fresh weather and rebuild the forecast |
| `pvstrings.add_geometry` | Record a new mounting geometry period |
| `pvstrings.rebase_shading` | Carry a string's shading observations over to a geometry corrected before v1.26.1 |
| `pvstrings.reset_learning` | Drop all learned corrections, keep measurements |
| `pvstrings.purge` | Delete raw 5-minute data past the retention period |
| `pvstrings.get_day` | One past day: forecast, day-ahead, baseline and measurement per hour, plus five-minute power (response only) |
| `pvstrings.get_weeks` | Day-ahead error week by week, against the baseline without learning (response only) |
| `pvstrings.backfill_irradiance_check` | Pair past irradiance readings with a reanalysis archive, to check the sensor |
| `pvstrings.new_irradiance_sensor` | Say the station was moved, replaced or cleaned — ends any running trial |
| `pvstrings.calibration_trial` | How the calibration trial is going, if one is running (response only) |

---

## Storage

One SQLite database per config entry, at `config/pvstrings/<entry_id>.db`,
separate from the Home Assistant recorder. All timestamps are Unix epoch UTC —
DST creates duplicate and missing local hours, which makes local time unusable
as a primary key.

Raw five-minute data is purged after the retention period (default 3 years).
Hourly aggregates, geometry history and model state are kept indefinitely; they
are tiny and they are the memory of the system.

### Diagnostics download

Settings → Devices & services → PV Strings → ⋮ → *Download diagnostics*. It is
deliberately verbose, because it is what tells "the forecast is wrong" apart
from "the data going in is wrong": the geometry history, the model state, the
collector counters, the scores, the last twenty-four hours of hourly rows, and
the configuration. Coordinates, elevation and the investment figure are
redacted. **The plant name, every entity id, and your tariff and feed-in prices
are not** — they are what the file is for. If your plant is named after your
street, so is the file; read it before attaching it to a public issue.
