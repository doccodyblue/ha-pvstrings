# TODO

Offene Arbeit an PVStrings. Stand 21.08.2026.
Erledigtes wandert raus, nicht ins Archiv — dafür gibt es den Changelog.

---

## Offen

### 1. Ersparnis und Amortisation auf AC rechnen, nicht auf DC

`coordinator._savings` (~Z. 897) füttert `store.energy_kwh_between` in
`econ.savings` — das ist gemessene **DC**-Strangenergie. Die Ersparnis
liegt damit systematisch um die Wandlungsverluste zu hoch (~5 % direct,
~7 % storage), die Amortisation entsprechend zu optimistisch.

Beim Nachprüfen mitgefunden: `core/economics.py:55-57` mischt schon
heute Einheiten — `self_used = yield_kwh (DC) − exported (AC vom
Zähler)`. Bei `net_metering` unsichtbar (alles × selber Preis), bei
`self_consumption` verzerrt es die Aufteilung.

Zu entscheiden beim Bauen:

- **direct**: AC ist gemessen (`conversion_5min.out_w` × 300 s) bzw.
  über die Kennlinie rechenbar. Beides vorhanden.
- **storage**: „AC" ist die falsche Zielgröße. Was zählt, ist die
  Entladung = Akkuladung × `discharge_efficiency` — konfiguriert, nicht
  messbar (siehe „Entschieden" unten). Als Schätzung kennzeichnen.
- **Stränge ohne Pfad**: bleiben DC. Im Attribut ausweisen, sonst mischt
  der Anlagenwert stillschweigend zwei Größen.
- **Historie**: Basiswechsel senkt den Lebenszeitwert rückwirkend um
  5–7 %. Neu durchrechnen (Rohdaten bleiben unangetastet, nur die
  Interpretation ändert sich).

### 2. Phase 4b — Kennlinien wirklich fitten

Gesammelt wird seit 21.08. (`conversion_5min`). Der Fit selbst fehlt:
Buckets über der Lastachse, Datenblatt als Prior, `n_eff` je Bucket,
Standby-Sockel aus den Daten statt geraten, `curve_source: learned`.
Attribut-Vertrag ist von der Dash-Session vorgegeben
(`handover-wandlung-lernen.md` dort): `curve_prior`,
`conversion_learning.bins`, `coverage`.

**Frühestens Anfang September** — der Sinn des Wartens ist, Streuung und
Bucket-Besetzung zu messen statt zu schätzen. Vorher nicht anfangen.

### 3. POA-Beam-Anteil exakt statt horizontal

`_beam_share` in `core/forecast.py` nutzt den horizontalen Beam-Anteil
`(GHI − DHI)/GHI`, nicht den der Panel-Ebene. Selbstbegrenzend (Lernen
und Anwenden benutzen dasselbe Maß), im Docstring dokumentiert. Exakter
Fix wäre, nur die POA-Beam-Komponente innerhalb der Physikkette zu
skalieren — Chirurgie an `physics.run`, kein Patch.

---

## In Beobachtung (kein Code, nur hinschauen)

- **S2-Erfolgskriterium**: erster klarer Vormittag, Stundenprognose
  9–13 Uhr innerhalb ±20 % der Messung (vor dem Umbau +80–100 %).
  Die 7-Tage-wMAPE darf sich dabei nicht über Rauschen hinaus
  verschlechtern.
- **AC-Prognose, 14 Tage**: gegen den gemessenen AC-Ertrag der
  direct-Gruppe, Tagessumme und stundenweise, Randstunden separat.
  Speicher-Pfad gegen die gemessene Ladeenergie.
- **Strang-Levels**: S2 lag nach dem Geometrie-Fix bei 0,79 gegenüber
  ~1,0 der Geschwister. Wenn das nach ein paar Tagen Live-Daten bleibt,
  ist entweder kWp optimistisch oder ein breiter Schatten leckt ins
  Level. Backfill-only-Werte nicht überinterpretieren.
- **Zellen-Dichte der Himmelskarte**: sollte sich mit echten
  POA-Beam-Zeilen wieder verdichten (~2 Wochen). Tut sie es nicht, ist
  die Shrinkage-Konstante dran.

---

## Entschieden — nicht neu aufrollen

- **`charge_efficiency` ist nicht lernbar.** Die Akku-Ladeleistung ist
  ein Nettofluss nach Hausverbrauch, kein Zweitor: gemessen 471 W
  batterieseitig gegen 58 W Ladung. Bleibt ein konfigurierter Wert.
- **Prognose-Clipping nur am Hardware-Nennwert**, nie an kommandierten
  oder rechtlichen Limits. Die Prognose ist Potenzial; Limits sind
  Regelung und gehören nach Node-RED.
- **Kein Lastmodell, keine Überschussprognose in der Integration.**
  Etablierte Arbeitsteilung.
- **`output_path` liegt auf der Gruppe**, nicht anlagenweit mit
  Override. Stränge ohne Gruppe bekommen keine Umrechnung und werden im
  Anlagen-AC-Sensor als `unconverted_strings` ausgewiesen.
- **Datenformat additiv migrieren**, nie „drop and recreate";
  Trainingsdaten überleben Updates.

---

## Release

Freeze aktiv seit 21.08. — committen und zu Andy deployen ja, taggen
nein. v1.20.0 wird gebündelt, wenn das S2-Kriterium bestanden und die
7-Tage-wMAPE etwa zwei Wochen stabil ist. Hotfix-Pfad: Branch vom
letzten Release-Tag, nur den Fix taggen.

Im Sammelkorb (unreleased auf `main`): per-Strang-`reset_learning`,
Conversion Layer, Messpaar-Sammlung, Sichtbarkeit konfigurierter Stufen.
