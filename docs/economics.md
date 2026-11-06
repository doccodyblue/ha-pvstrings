# Savings and tariffs

*[README](../README.md) · [Setup](setup.md) · [Sensors](sensors.md) · [How it works](how-it-works.md) · [Shading](shading.md) · [Accuracy](accuracy.md) · [Curtailment](curtailment.md) · [Savings](economics.md) · [Services](services.md) · [Status](status.md)*

The savings figures run on measured data only, so they are valid before the
learning layer is warm.

## What is counted

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

## Time-of-use tariffs

Two optional fields take a sensor carrying your current price — one for import,
one for feed-in. Point them at Tibber, aWATTar, Octopus, Nord Pool or anything
else that publishes a price, and every hour is then valued at the price that
was recorded while it ran, rather than at one number for all of them.

Leave them empty and nothing changes: the fixed import and feed-in prices from
the options are used for everything, exactly as before.

On a fixed schedule rather than a market tariff, build one template helper per
side (Settings → Devices & services → Helpers → Template → Template a sensor),
with the unit set to your currency per kWh:

```jinja
{% set h = now().hour %}
{% if h < 6 %}0.165
{% elif 11 <= h < 14 %}0.00
{% elif 15 <= h < 21 %}0.625
{% else %}0.285{% endif %}
```

Home Assistant re-evaluates templates containing `now()` every minute, so
nothing else is needed. The unit is read from the sensor — `EUR/kWh`, `ct/kWh`,
`p/kWh`, `EUR/MWh` are all understood, and a sensor whose unit cannot be read
as a price per energy is refused during setup rather than silently valued a
hundredfold wrong.

Three things worth knowing about the result:

- Hours from before the sensor was configured, and any gap in it, fall back to
  the fixed price. `price.by_basis_kwh` in the savings attributes says how much
  of the total rests on which.
- The hour is the resolution of the record. A tariff switching price on the
  half hour is valued at that hour's mean.
- Negative prices are applied as they stand. An hour with a negative import
  price makes self-consumption worth less than nothing, and the figure says so.

Export is capped at what the strings delivered **in the same hour**. Anything
beyond that — a battery discharging to the grid, a second generator behind the
meter, a reversed meter sign — is reported as `export_dropped_kwh` instead of
being credited to PV.

## Tariff models

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

That weighting assumes a kilowatt-hour is worth the same in December as in
June, which stops being true under a time-varying tariff. While a price sensor
is recording and less than a full year has been observed, the estimate carries
`annual_estimate_caveat: time_of_use`; once a year is on the record the
weighting is a no-op and the mark disappears on its own.

Amortisation runs on the same delivered energy, so its target date moves out by
however much your conversion losses are — typically 4–6 % for a direct path,
around 10 % through a battery.
