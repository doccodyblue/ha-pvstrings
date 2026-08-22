# TODO

Offene Arbeit an PVStrings. Stand 22.08.2026.
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

### 2. Kennlinien-Lernen: eine Konstante ist noch geraten

Stufe B ist gebaut (22.08.). Die Bucket-Frage hat sich erledigt — nicht
durch Nachjustieren, sondern strukturell: Schrumpfung nach Evidenz
statt harter Schwelle lässt unerreichte Stützstellen exakt auf dem
Datenblatt und macht den Übergang stufenlos, für jede Anlagengröße
ohne Konfiguration.

Offen bleibt **`STANDBY_FLOOR_PCT`** (1 %): der Eigenverbrauch des
Wechselrichters ist messbar, sobald genug Schwachlast-Paare da sind —
dann die Schwelle aus den Daten setzen statt aus dem Bauch. Ablesbar an
`spread` und `measured` der untersten Buckets in `conversion_curves`.

### 2b. MPPT-Stufe lernt noch nicht

Die Paare werden gesammelt, angewendet wird weiter ein fester Faktor.
Grund: gemessen 0,972/0,971 gegen konfigurierte 0,97 — der Pauschalwert
stimmt fast, und eine Kurve daraus zu machen hieße `convert_storage`
umzubauen. Lohnt sich erst, wenn die Residuen es rechtfertigen.

### 3. POA-Beam-Anteil exakt statt horizontal

`_beam_share` in `core/forecast.py` nutzt den horizontalen Beam-Anteil
`(GHI − DHI)/GHI`, nicht den der Panel-Ebene. Selbstbegrenzend (Lernen
und Anwenden benutzen dasselbe Maß), im Docstring dokumentiert. Exakter
Fix wäre, nur die POA-Beam-Komponente innerhalb der Physikkette zu
skalieren — Chirurgie an `physics.run`, kein Patch.

### 4. ~~Nowcast~~ — gebaut am 22.08., in Beobachtung

Umgesetzt als kt-Persistenz (`core/persistence.py`), nicht als
Verhältnis-Persistenz wie zuerst geplant: die Kalibrierung über 11 Tage
hat die ursprüngliche Entscheidung widerlegt. Halbwertszeit 70 min bei
ruhigem, 31 min bei aufgerissenem Himmel, Schnitt bei 120 min.

Offen geblieben und beim ersten Winter zu prüfen: die Konstanten
stammen aus Hochsommerdaten. Die Regime-Trennung sollte das auffangen
(Hochnebel ist per Konstruktion „ruhig"), belegt ist es nicht.
Ablesbar an `nowcast_sky` und `nowcast_halflife_min` am
Einstrahlungssensor.

<details>
<summary>Ursprüngliche Analyse</summary>

Beobachtet am 22.08. Die Tagesprognose stand um 11:08 bei 9,96 kWh,
während der GW2000A bereits mehr Einstrahlung sah als die Vorhersage
(um 16:31 noch immer 261 gegen 169 W/m² prognostiziert). Nach oben
korrigiert wurde erst um 13:02, als der Wetteranbieter nachzog — die
12-Uhr-Stunde war da vorbei und bleibt mit 1,87 statt 2,38 kWh stehen.
Tagesbilanz: 12,37 kWh prognostiziert, 13,80 kWh geerntet.

Der Messwert wird bereits gelesen (`_measured_ghi`), fließt aber nur
ins Langzeit-Bias (`_learn_ghi_bias`, gebuckettet nach Stunde und
Horizont) und in die Verschattungskarte — nicht vorwärts in denselben
Tag. Vorschlag: Persistenz auf dem Klarheitsindex, also gemessenes kt
der letzten 15–30 min über die nächsten ein bis zwei Stunden
einblenden und exponentiell zur Prognose zurückführen. Setzt dort an,
wo `_downscale` kt ohnehin schon als Erhaltungsgröße behandelt.

Zu klären beim Bauen:

- Blendfenster und Halbwertszeit — an Andys und Wagners Historie
  kalibrierbar, beide haben einen Einstrahlungssensor.
- Wolkenlücken: kt springt zwischen zwei Messungen um Faktoren. Ein
  Median über mehrere Intervalle statt des letzten Werts.
- **Nur die Restprognose darf sich bewegen.** Vergangene Stunden
  bleiben eingefroren, sonst rechnet sich das Scoring die Trefferquote
  schön. `log_forecast` läuft heute vor der Gruppenaggregation, der
  Pfad ist sauber getrennt — beim Umbau muss er es bleiben.
- Anlagen ohne Sensor: unverändertes Verhalten. Kein Fallback über die
  gemessene Leistung, die ist verschattungs- und drosselungsbehaftet.

</details>

### 5. Restprognose zählt die angebrochene Stunde ganz mit

`ForecastData.remaining_kwh` (`coordinator.py:214`) summiert ab
`floor_hour(now)`. Um 16:31 steckten die vollen 0,654 kWh der
16-Uhr-Stunde im ausgewiesenen Rest, obwohl gut die Hälfte davon
bereits erzeugt war — rund ein Drittel des Werts zu hoch. Betrifft
Anlagen-, Gruppen- und Strangsensoren gleichermaßen und trifft jeden,
der daran eine Lastentscheidung aufhängt.

Linear anteilig zu rechnen wäre der billige Fix, ist aber nahe
Sonnenauf- und -untergang genau der Fehler, den `_downscale` für die
Prognose schon vermeidet. Sauberer: die Fünf-Minuten-Serie bis zur
Rest-Berechnung durchreichen, statt vorher auf Stunden zu falten.

### 6. Prognose und Restprognose sehen aus wie zwei Summanden

Andy und Wagner haben unabhängig voneinander dieselbe Kachel so
gelesen, dass beide Zahlen addiert die Tageserwartung ergeben.
Tatsächlich ist der Rest eine Teilmenge der Tagessumme. Zwei
Fehlleser aus zwei Installationen sind kein Zufall, sondern eine
Aussage über die Benennung.

- Integration: Attribut auf `forecast_remaining`, das die Beziehung
  ausspricht (Teilmenge von `forecast_today`, dazu der verstrichene
  Anteil) — im Stil des `semantics`-Attributs der AC-Sensoren.
  Umbenennen wäre ein Bruch für bestehende Dashboards und
  Automationen, kommt im Freeze nicht in Frage.
- Dashboard: Beschriftung „davon noch offen" statt „Restprognose
  heute". Gehört ins Nachbarprojekt, per `handover.md`.

Mit aufschreiben, weil es beim Erklären gebraucht wurde: „Prognose
heute" ist kein Tagesendwert, sondern vergangene Stunden eingefroren
plus lebende Restprognose. Sie holt die Realität nie rückwirkend ein —
und soll das auch nicht, sonst wäre die Trefferquote wertlos.

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
