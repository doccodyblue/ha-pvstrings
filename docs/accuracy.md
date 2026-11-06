# Accuracy and diagnostics

*[README](../README.md) · [Setup](setup.md) · [Sensors](sensors.md) · [How it works](how-it-works.md) · [Shading](shading.md) · [Accuracy](accuracy.md) · [Curtailment](curtailment.md) · [Savings](economics.md) · [Services](services.md) · [Status](status.md)*

## Error figures

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
error figure. Expect the day-ahead number to sit well above the nowcast one:
that gap is the honest cost of forecasting a day ahead.

Every figure here is an error: **0 would be a perfect forecast, and lower is
better.** The day-ahead sensors were called *accuracy* until 1.24, and a
reader took 31.6 % to mean "about a third right" — the opposite of what it
says. It means the day-ahead forecasts missed, in total, by a third of what
came.

The 30-day day-ahead sensor also publishes the pairs behind its number: a
`history` of announced-versus-measured per day, plant and per string, and an
`hourly_profile` of the same pairs folded by local hour of day. The first
draws the daily chart; the second says whether the error lives in the morning
or the afternoon, which is what a windowed automation needs to know (see
[Reading one window out of the hourly series](sensors.md#summing-a-time-window)).

### History by day and week

Two read-only services answer with data instead of writing it anywhere, so
nothing lands in the recorder. The dashboard uses them to step back through
past days and weeks.

`get_day` returns one past local day per hour and string: the forecast as it
stood when the hour began, the day-ahead figure and which run it came from,
the measurement with its censoring, and the five-minute power while the raw
rows still exist. Day-ahead figures exist for as long as their issues are
kept (35 days); older days show the forecast and the measurement only.

`get_weeks` returns each local week's day-ahead error as sums — forecast,
actual, and the absolute error per day — so a chart can add weeks up. The
same week is also scored against a **baseline: this integration with every
learned layer switched off** (no source bias, no nowcast, no sky map, no
correction factor) on the same weather run. It is not "pure physics" in any
stronger sense, and the distance between the two is what learning has saved.
Weeks are stored when they close, because the day-ahead issues they need are
gone a few weeks later. Each week carries the hourly profile, plant and per
string; weeks closed before 1.27.1 have the plant profile only and are not
rebuilt, since the evening runs they were scored against are gone.

The baseline is logged from 1.25 on. The weeks already in the issue window
on the first start are rebuilt once and marked `backfilled`: their day-ahead
figures are the ones published then, but their baseline is replayed with
today's code and today's geometry, and there is no record of how mature the
model was.

### Weather source vs. model

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

All three are ratios of daily sums, exactly like the error sensors
themselves — a split computed over hours comes out two to three times larger
for the same data and would read as a worse plant rather than as an
explanation of one.

The two parts are absolute errors and deliberately do **not** add up to the
whole: an over- and an under-shoot cancel within a day and cannot cancel
between the parts. The chain figure also flatters itself slightly, because the learned
correction inside it was fitted on those very hours — it is a regression signal
for development, not a claim of accuracy on unseen days.

**Without an irradiance sensor** nothing here changes and nothing breaks: no
counterfactual is computed, no extra work is done, and the sensor states plainly
that no sensor is configured rather than showing an empty tile. Everything else
in the integration works exactly as before.

## Irradiance sensor check

Only if you have one. Cheap weather stations measure illuminance with a diode
weighted to human vision and divide by a fixed constant, so they read
progressively lower as the sun drops. Every closed hour is compared against
two reanalysis archives, grouped by sun height. On the reference plant:

```
sun  3-15°   0.597      sun 35-45°   0.749
sun 15-25°   0.654      sun 45-90°   0.775
sun 25-35°   0.726
```

The result is the `sensor_check` attribute on the irradiance forecast sensor.
Fetch the archive once with `pvstrings.backfill_irradiance_check` — by hand,
because only you know whether the sensor stood still for those months.

**Nothing is corrected.** The nowcast, the bias model and the shading map
absorbed part of this error already, so a factor in front of them would
correct it twice. Instead, where the evidence supports a curve, a second
model learns from scratch through it and is scored against the published one;
after fifteen clear days a criterion says whether it was better.
`pvstrings.calibration_trial` reports where it stands.

The check runs everywhere and costs a few rows. The trial is a separate
option, off by default: it spends a second forecast and a second learn pass
an hour, and that is not something to take from someone who did not ask. An
accurate sensor produces a flat curve and no trial even when switched on.
