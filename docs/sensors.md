# Sensors and attributes

*[README](../README.md) · [Setup](setup.md) · [Sensors](sensors.md) · [How it works](how-it-works.md) · [Shading](shading.md) · [Accuracy](accuracy.md) · [Curtailment](curtailment.md) · [Savings](economics.md) · [Services](services.md) · [Status](status.md)*

## Entities

Per plant:

| Sensor | Notes |
|---|---|
| Forecast today / remaining / tomorrow | hourly detail in the `forecast` attribute; *remaining* is a **subset of today**, see [below](#remaining-vs-today) |
| Forecast next hour, peak hour today | |
| Produced today | measured, from the integration's own 5-minute data |
| Deviation yesterday | what the evening-before forecast said, vs. actual |
| Day-ahead error 7 d | how wrong "tomorrow" actually is — 0 % would be perfect; see [Accuracy](accuracy.md) |
| Day-ahead error 30 d, day-ahead bias 30 d | diagnostic |
| WMAPE 7 d / 30 d, Bias 7 d | diagnostic; nowcast quality, see [Accuracy](accuracy.md) |
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

### Remaining vs. today

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

### State classes

The measured ones — produced today, the savings figures — do, so they work in
the Energy dashboard and in long-term statistics. The forecast sensors
deliberately do not: a prediction that rises and falls through the day is not a
meter reading, and letting the recorder accumulate statistics from it produces
a number with no meaning. Home Assistant's own solar-forecast integration does
the same.

## Using the data in automations

Every value is an ordinary entity with ordinary attributes — nothing is
locked inside a custom card. The hourly series sits in the `forecast` attribute,
templates and automations read it like any other sensor, and the per-group
*Remaining forecast* exists precisely so a controller can ask how much is still
to come behind one inverter before it decides to throttle or divert.

That is the intended use: this integration answers *what the plant could
produce*, and leaves *what to do about it* to your automations.

### Summing a time window

Every forecast sensor — plant, string and group, today, tomorrow and the day
after — carries a `forecast` attribute: one entry per hour with `datetime` and
`potential_kwh`. A window is a template away. This one answers *"how much will
arrive tomorrow morning before 11:00"*, which is the question a battery
controller asks the evening before:

```jinja
{% set hours = state_attr('sensor.YOUR_PLANT_forecast_tomorrow', 'forecast') or [] %}
{% set ns = namespace(total=0.0) %}
{% for row in hours %}
  {% set local = as_local(as_datetime(row.datetime)) %}
  {% if 5 <= local.hour < 11 %}
    {% set ns.total = ns.total + row.potential_kwh %}
  {% endif %}
{% endfor %}
{{ ns.total | round(2) }}
```

Two things to get right. The timestamps are **UTC** — `as_local` is not
decoration, and skipping it shifts every hour by your offset. And
`potential_kwh` is the strings' DC potential, before inverter or charge
losses; where a group has an output path configured, its own sensor publishes
the converted figure instead.

There are deliberately no per-hour entities. Everybody's window is different —
sunrise to 11:00, or 14:00 to 18:00 — and a template sensor gives you exactly
yours, as many as you like, without adding twenty-four entities to everyone
else's registry.

If the number drives a decision with a cost attached, pair it with the
day-ahead error sensors: they say how wrong *your* plant's day-ahead
forecast has been over the last 7 and 30 days, which is the honest margin to
plan with.

A daily figure cannot say *where in the day* the error sits, and for a window
that is the whole question — a forecast that runs hot in the morning and cold
in the afternoon can score a fine day and still leave a battery empty at
09:00. The 30-day day-ahead sensor therefore also carries an `hourly_profile`:
the same scored pairs folded by local hour, plant-wide, one entry per hour
with `forecast_kwh`, `actual_kwh` and the number of `days` behind it. Summed
over your own window it is the margin that window needs:

```jinja
{% set profile = state_attr('sensor.YOUR_PLANT_day_ahead_error_30_days', 'hourly_profile') or [] %}
{% set ns = namespace(forecast=0.0, actual=0.0) %}
{% for row in profile if 5 <= row.hour < 11 %}
  {% set ns.forecast = ns.forecast + row.forecast_kwh %}
  {% set ns.actual = ns.actual + row.actual_kwh %}
{% endfor %}
{{ ((ns.forecast - ns.actual) / ns.forecast * 100) | round(1) if ns.forecast > 0 else 'unknown' }}
```

Positive means the morning was announced too high, by that share; plan with
it as a discount on tomorrow's window sum. The hours are already local, so no
`as_local` here. Unlike the error figures, the profile is not held back
until three days are in — the `days` field says how thin the basis is, and
thin is what a fresh installation has to work with.

Next to it, `hourly_profile_by_string` holds the same fold per string, keyed
by string id (`strings_detail` maps names to ids). Errors of opposite sign
cancel in the plant sum — one string shaded in the morning, another facing
east — and only show here. Per hour the strings add up to the plant. Both
profiles count every paired hour, including those an inverter limit cut
short; hours without a measurement are left out. The per-string profile is
not written to the recorder: next to the daily history it would push the
entity toward the recorder's size limit.

Installations set up before 1.24 keep the entity id they were registered
with, `sensor.YOUR_PLANT_day_ahead_accuracy_30_days`; only the display name
changed. Home Assistant never renames an entity id behind your automations.
