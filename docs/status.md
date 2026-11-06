# Status and limits

*[README](../README.md) · [Setup](setup.md) · [Sensors](sensors.md) · [How it works](how-it-works.md) · [Shading](shading.md) · [Accuracy](accuracy.md) · [Curtailment](curtailment.md) · [Savings](economics.md) · [Services](services.md) · [Status](status.md)*

## Status

Running on two installations since 11 August 2026, and developed against Home
Assistant 2026.8. The 2025.9 floor was established by checking that the required
APIs exist there, not by running it. Breaking changes remain possible between
minor versions before 2.0; the schema migrates in place, and the v2 → v3 upgrade
has been exercised against a real database.

1036 automated tests cover the physics chain, the learning rules, censoring, the
sky map, storage and the config-flow schemas — including the parts that only
fail against a real Home Assistant.

The model does need time, and says so rather than guessing: no error figure is
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

Questions, ideas and screenshots of your own plant belong in
[Discussions](https://github.com/doccodyblue/ha-pvstrings/discussions); every
plant teaches the model something different, and seeing them is the best test
there is. Issues are for bugs and feature requests.

## Out of scope

- No `usable_kwh`. Reporting how much of the potential you can actually *use*
  needs a load forecast and a time-resolved battery simulation. Both are their
  own subproject; guessing would be worse than not answering. Only
  `potential_kwh` is published. The economics run on measured values and are
  unaffected.
- No neural networks or tree ensembles, no champion/challenger, no multi-source
  blending, no snow model.
- The tariff layer values the past and nothing else. No forecast is priced, and
  nothing here decides when to charge a battery because power is cheap at
  noon — that is an automation, and it belongs where the knowledge about your
  house lives.
