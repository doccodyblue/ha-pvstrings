# Shading and the sky map

*[README](../README.md) · [Setup](setup.md) · [Sensors](sensors.md) · [How it works](how-it-works.md) · [Shading](shading.md) · [Accuracy](accuracy.md) · [Curtailment](curtailment.md) · [Savings](economics.md) · [Services](services.md) · [Status](status.md)*

The sky is divided into cells of ten degrees of azimuth by five of elevation,
and each string learns its own map of them, from every clean five-minute
interval it was measured in. Indexed on the sun rather than the clock, because
a chimney's shadow sits at a fixed place in the sky while the time it arrives
drifts by an hour twice a year.

On a plant with two or more strings the maps are fitted **jointly**, and that
is what separates a shadow from the weather. Each observation is read as
`level(string) + moment(timestamp) + shade(cell)` in log space: the moment is
whatever every string saw at the same time — a cloud edge the irradiance source
missed, enhancement off a bright cumulus, physics running low across the site —
and it cancels between siblings, because only the shade belongs to one string's
patch of sky. Level and moment are estimated and thrown away; the map keeps the
shape. This is what lets a shadow be measured on a string that beats physics
everywhere: against an absolute reference such a string looks flawless, against
its siblings the missing morning is plain. A single-string plant has nobody to
difference against and keeps the absolute envelope.

What it will not do:

- **Invent sky it has not seen.** In August the sun never reaches the winter
  cells, so those correct nothing. A complete map takes a full turn of the
  seasons.
- **Read the horizon without a second opinion.** Below eight degrees the chain
  is at its least trustworthy: the DC model is linear where real modules are
  not, the transposition and incidence models are at their limits, and the
  irradiance source is at its weakest on the direct/diffuse split. On a plant
  with two or more strings the joint fit can still use it — whatever the whole
  site got wrong in that moment cancels between siblings, and what survives is
  applied scaled by the beam share. A string with nobody to difference against
  has neither, so its map is fitted from eight degrees up. The observations
  below stay on disk and start counting the day that string gets a sibling.
- **Carry the string's overall level.** The map is normalised against parity,
  so it reports *shape* — how a string varies across the sky — and leaves the
  level to the per-string log-ratio layer. The cost is real and known: on a
  string whose physics runs low everywhere, a shadow that does not push a cell
  below parity stays invisible.
- **Learn from an interval it should not.** Curtailed intervals, a full
  battery, a charge controller holding a voltage — all are excluded, because a
  throttled afternoon recurs at the same sun positions as a shadow does and
  would otherwise be learned as one.
- **Subtract a shadow from an overcast hour.** A cell holds a clear-day loss,
  and it is applied scaled by the POA beam share of the moment: under a closed
  sky the obstacle took nothing, so nothing is taken off. The same share weights
  an observation as it is learned, so a grey morning cannot vote a shadow away —
  and a loss seen at half beam is not filed as if the day had been clear.
  The *shading now* sensor follows the same rule: it scales the cell's
  clear-day loss by the beam share of the moment — reconstructed from the
  irradiance sensor's last quarter hour where the nowcast finds it usable,
  from the weather source's split otherwise. Its attributes carry
  `clear_day_factor` (the loss on a clear day at this sun position),
  `beam_share` with its `beam_source` (`measured` or `forecast`), and the
  source's `forecast_beam_share` next to it. Where no share can be computed
  the full clear-day loss stays — ignorance never reads as "no shade".

Each cell reports its own `ratio` — what the string actually did there,
measured over physics, before any normalisation — and each map reports the
`level` the joint fit found for that string and the `fit_method` that produced
it. Without them a map showing no loss anywhere is unreadable: nothing in the
way and everything equally in the way draw the same picture, and they want
opposite repairs.
