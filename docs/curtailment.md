# Output paths and curtailment

*[README](../README.md) · [Setup](setup.md) · [Sensors](sensors.md) · [How it works](how-it-works.md) · [Shading](shading.md) · [Accuracy](accuracy.md) · [Curtailment](curtailment.md) · [Savings](economics.md) · [Services](services.md) · [Status](status.md)*

## Output paths (DC, AC, battery)

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
