# PV-Strings — Upgrade: Conversion Layer & AC-Prognose

**Status:** Anforderung / Handoff an Claude Code
**Datum:** 2026-08-21
**Betrifft:** Prognose-Kern, Kraftwerksmodell, Config Flow, bereitgestellte Entities
**Nachgelagert:** `ha-pvstrings-dashboard` (separates Projekt, siehe Abschnitt 9)

---

## 0. Harte Randbedingungen — vor allem anderen lesen

Die aktuelle Version ist **bereits produktiv ausgerollt**. Daraus folgen drei nicht verhandelbare Constraints:

### 0.1 Non-Breaking
Jede Änderung muss abwärtskompatibel sein. Wenn eine Änderung nicht ohne Breaking Change umsetzbar ist: **stoppen und Rücksprache halten, nicht selbst entscheiden.** Das gilt für Entity-IDs, Konfigurationsschlüssel, Attributnamen, Sensor-Semantik und Speicherformate.

Konkret verboten ohne Rücksprache:
- Umbenennen oder Entfernen bestehender Entities
- Ändern der Bedeutung eines bestehenden Sensors (z. B. DC-Sensor liefert plötzlich AC-Werte — bricht nichts sichtbar und ist genau deshalb gefährlich)
- Ändern von Einheit, `device_class` oder `state_class` bestehender Entities
- Konfigurationsschlüssel umbenennen oder zu Pflichtfeldern machen
- Änderungen am Persistenzformat ohne Migrationspfad

Nach dem Update ohne jede Nutzeraktion muss gelten: identisches Verhalten wie vorher. Alle neuen Stufen sind per Default neutral (Wirkungsgrad 1.0, Clipping aus, Lernen aus).

### 0.2 Trainingsdaten überleben das Update
Bestehende Trainingsdaten dürfen bei einem Update **unter keinen Umständen gelöscht, überschrieben oder stillschweigend invalidiert werden.**

- Schema-Änderungen nur mit expliziter Migration, nie mit „drop and recreate"
- Vor der Migration eine Sicherung des alten Datenbestands anlegen
- Migration muss idempotent sein und einen fehlgeschlagenen Lauf überstehen, ohne den Ausgangsbestand zu beschädigen
- Falls neue Felder benötigt werden: additiv ergänzen, Altdatensätze mit `NULL`/Default füllen statt sie zu verwerfen
- Version des Datenformats mitschreiben, damit künftige Migrationen wissen, womit sie es zu tun haben
- Falls sich das Trainingsziel ändert (siehe Phase 5): Altdaten **behalten** und markieren, nicht löschen. Ein Zielwechsel ist kein Grund, Historie wegzuwerfen.

### 0.3 Alles dynamisch konfigurierbar über den Config Flow
Keine hartcodierten Anlagenparameter, keine YAML-only-Optionen. Alles, was dieses Upgrade an Parametern einführt, muss der Nutzer über den bestehenden Config Flow bzw. Options Flow setzen können — siehe Abschnitt 5.

---

## 1. Kontext & Auslöser

PV-Strings prognostiziert aktuell **DC-Leistung** — also das, was die Panels liefern. Physikalisch die saubere Größe, weil sie direkt aus Einstrahlung, Ausrichtung, Temperatur und Verschattung folgt.

Beim Abgleich mit dem Home-Assistant-Energie-Dashboard fällt eine systematische Abweichung auf. Kein Bug: das Energie-Dashboard zeigt AC-Energie hinter dem Wechselrichter, PV-Strings zeigt DC davor. Dazwischen liegen Wandlungsverluste, die lastabhängig sind.

Für die Nutzersicht ist AC die relevante Größe: die Frage lautet „wie viel kWh kann ich heute noch verbrauchen oder einspeichern". Zusätzlich soll die AC-Prognose als Entity extern referenzierbar sein (siehe 2.5).

---

## 2. Entscheidungen

1. **Das DC-Modell bleibt der Kern.** Es wird nicht ersetzt, sondern bekommt einen Layer obendrauf.
2. **PV-Strings stellt beide Größen als Entities bereit — DC und AC.** Welche angezeigt wird (oder beide), entscheidet ausschließlich die Dashboard-Logik im separaten Projekt.
3. **Der Conversion Layer gehört in PV-Strings**, nicht in eine externe Integration. PV-Strings kennt die Kraftwerkstopologie bereits; eine externe Lösung müsste sie ein zweites Mal pflegen — garantierte Drift-Quelle.
4. **Der Batteriepfad wird nicht als Wirkungsgradkette modelliert, sondern als Zeitversatz.** Siehe 3.4.
5. **Die AC-Prognose wird als eigenständige, stabil benannte Entity veröffentlicht**, damit sie extern (u. a. vom ZERO-Controller) referenziert werden kann. Der ZERO-Controller selbst wird nicht angefasst.

---

## 3. Fachliches Modell

### 3.1 Zwei Kraftwerkstypen, zwei Prognosegrößen

| Typ | Pfad | Prognosegröße |
|---|---|---|
| `direct` | DC → WR → AC → Netz/Haus | **prognostizierte AC-Einspeisung heute/morgen** |
| `storage` | DC → externer MPPT → Akku → (später) WR → AC | **prognostizierte nutzbare Akkuladung heute/morgen** |

Der Knackpunkt bei `storage`: es gibt **keine sinnvolle Kette** von Sonnenenergie zu ausgespeister AC-Energie, weil der Akku dazwischensitzt. Energie von 13:00 kann um 22:00 ausgespeist werden — oder gar nicht.

### 3.2 Conversion Layer — Stufen

Pro Kraftwerk eine Kette optionaler, komponierbarer Stufen:

- **`inverter_efficiency`** — lastabhängige Kennlinie, vorbelegt aus Datenblatt, im Betrieb lernfähig (siehe 3.3)
- **`mppt_efficiency`** — pauschaler Konstantwert, **nur für externe MPPT-Regler** (siehe 3.3.1). Nicht lernfähig, nicht spannungsabhängig. Verfeinerung später, falls die Residuen es rechtfertigen.
- **`battery_charge_efficiency` / `battery_discharge_efficiency`** — getrennt geführt, nicht als ein Round-Trip-Wert (siehe 3.4)

### 3.2.1 Achtung: MPPT nicht doppelt zählen

Beim Dach-Kraftwerk ist ein **Hoymiles HMS-1600-4T** verbaut. Dessen MPPT-Tracker sind **im Mikrowechselrichter integriert** — die MPPT-Verluste stecken bereits in der Wirkungsgradkennlinie des Geräts. Für `direct`-Kraftwerke mit Mikrowechselrichter darf deshalb **keine separate MPPT-Stufe** konfiguriert werden, sonst wird derselbe Verlust zweimal abgezogen.

Die pauschale MPPT-Stufe gilt ausschließlich für **externe Laderegler** — beim Speicher-Kraftwerk die beiden Victron-MPPTs, die als eigenständige Geräte vor dem Akku sitzen.

Das Schema soll diesen Fehler abfangen: `direct` + Mikrowechselrichter + separate MPPT-Stufe → Warnung bei der Validierung.

### 3.3 Wechselrichter-Kennlinie: vorbelegt, dann lernend

Ein konstanter Wirkungsgrad wäre grob falsch. Der Wirkungsgrad bricht im unteren Lastbereich deutlich ein, und dort fällt ein erheblicher Teil des Jahresertrags an (Morgen-/Abendstunden, Winter, bedeckte Tage).

**Stufe A — Vorbelegung aus Datenblatt.** Jeder unterstützte Wechselrichtertyp bekommt eine im Repo hinterlegte Default-Kennlinie. Greift ab Tag eins, ohne Trainingsdaten. Ausgangswerte siehe 8.1.

**Stufe B — Lernfähige Korrektur im Betrieb.** Sobald gepaarte DC-/AC-Messreihen vorliegen, wird die Kennlinie an die reale Anlage angepasst. Damit werden Alterung, Betriebstemperatur, Leitungsverluste und Exemplarstreuung implizit mitgelernt.

Randbedingungen für Stufe B:
- **Default: aus.** Nutzer aktiviert es bewusst über den Options Flow (Constraint 0.1).
- Gelernte Kennlinie **monoton plausibel und gedeckelt** — harte Grenzen pro Stützstelle (Default ±5 Prozentpunkte um den Datenblattwert), damit ein Sensorausfall die Kurve nicht zerlegt
- **Persistieren**, nicht bei jedem Neustart neu lernen. Datenblattwert bleibt als Fallback und Reset-Ziel.
- Nur Zeitfenster mit validen Trainingsdaten (insbesondere Clipping-Phasen ausschließen, siehe 3.5)
- Mindestanzahl Datenpunkte pro Laststützstelle, bevor der gelernte Wert den Datenblattwert überschreibt
- Gelernte vs. vorbelegte Kennlinie muss **inspizierbar** sein (Diagnose-Attribut oder Log), sonst ist eine Abweichung später nicht diagnostizierbar
- Reset-Möglichkeit auf Datenblattwerte über den Options Flow
- Gelernte Kennlinien fallen unter Constraint 0.2: sie überleben Updates

### 3.4 Batteriepfad = Zeitversatz, nicht Wirkungsgrad

Für `storage`-Kraftwerke endet die Prognose an der Akku-Klemme. Was der Wechselrichter daraus wann herausschiebt, ist eine **Entladeentscheidung des Merit-Order-/ZERO-Controllers** — Regelung, kein Prognoseproblem. PV-Strings soll das nicht raten.

Lade- und Entladewirkungsgrad werden **getrennt** geführt, nicht zusammengefasst. Gründe: Ladepfad (DC-seitig, MPPT → Akku) und Entladepfad (Akku → WR → AC) sind physikalisch verschiedene Stufen, unabhängig messbar, und nur der Ladepfad gehört in die Prognose. Round-Trip bleibt als abgeleiteter Wert für Plausibilitätsprüfungen.

Konsequenz: die beiden Kraftwerkstypen haben unterschiedliche Einheiten-Semantik („erwartete Einspeisung" vs. „erwartete Akkuladung"). Muss über Entity-Namen und Attribute klar transportiert werden.

### 3.5 Clipping

**Ist-Zustand:** Clipping wird bislang ausschließlich dazu verwendet, **Trainingsdaten zu invalidieren** — bei Clipping ist die gemessene DC-Leistung nicht mehr deckungsgleich mit der theoretisch möglichen Solarleistung und taugt nicht als Trainingsziel.

**Neu:** Clipping muss zusätzlich **im Prognosepfad modelliert** werden. Die AC-seitige Begrenzung am Wechselrichter-Nennwert gehört in den Conversion Layer, sonst prognostiziert PV-Strings zur Mittagsspitze systematisch zu hoch. Bei asymmetrischer Auslegung real relevant.

Die beiden Verwendungen sind **strikt zu trennen**:
- *Trainingsdaten-Invalidierung*: bestehende Logik, bleibt unverändert
- *Prognose-Clipping*: neue Logik im Conversion Layer, wirkt auf die vorhergesagte AC-Zeitreihe

Kein Rückkanal von der Prognose in die Datenvalidierung — sonst validiert sich die Prognose irgendwann selbst.

---

## 4. Konfiguration (Datenmodell)

Skizze — Feldnamen an bestehende Repo-Konventionen anpassen. Alle Werte kommen aus dem Config Flow, nicht aus statischem YAML:

```yaml
plants:
  - id: dach
    output_path: direct
    strings: [dach_ost, dach_west]
    conversion:
      inverter:
        model: hoymiles_hms1600_4t     # lädt Default-Kennlinie aus inverter_models/
        rated_ac_w: 1600
        integrated_mppt: true          # → keine separate MPPT-Stufe erlaubt
        clipping: true
        learning:
          enabled: false               # Default aus
          max_deviation_pp: 5
          min_samples_per_bin: 50

  - id: garage
    output_path: storage
    strings: [garage_sued, garage_ost]
    conversion:
      mppt:
        efficiency: 0.97               # externe Victron-Regler, pauschal
    storage:
      charge_efficiency: 0.96
      discharge_efficiency: 0.96
      discharge_controlled_by: external
```

Kennlinien als eigene Dateien pro Wechselrichtermodell im Repo (z. B. `inverter_models/hoymiles_hms1600_4t.yaml`), **nicht hartcodiert**. Laststützstellen in Prozent der Nennleistung, lineare Interpolation, keine Rundung auf Stützstellen.

---

## 5. Config Flow / Options Flow

Alles Neue muss über die HA-Oberfläche konfigurierbar sein. Anforderungen:

**Migration bestehender Config Entries**
- `VERSION` der Config Entry hochziehen, `async_migrate_entry` implementieren
- Bestehende Einträge werden **automatisch** mit neutralen Defaults ergänzt — kein Zwang zur Neukonfiguration, keine kaputte Integration nach dem Update
- Fehlgeschlagene Migration darf die Entry nicht beschädigen

**Neue Optionen im Options Flow**
- Pfadtyp pro Kraftwerk (`direct` / `storage`)
- Wechselrichtermodell aus Auswahlliste, plus Option „benutzerdefiniert" mit manueller Kennlinieneingabe
- Nennleistung AC, Clipping an/aus
- MPPT-Wirkungsgrad (nur sichtbar bei externem MPPT)
- Lade-/Entladewirkungsgrad (nur sichtbar bei `output_path: storage`)
- Kennlinienlernen an/aus, Deckelung, Mindest-Samplezahl
- Reset gelernter Kennlinien auf Datenblattwerte

**Bedienbarkeit**
- Kontextabhängige Formulare: bei `direct` keine Speicherfelder anzeigen, bei integriertem MPPT kein MPPT-Feld
- Sinnvolle Wertebereiche und Validierung (Wirkungsgrad 0,5–1,0; Nennleistung > 0)
- Bestehende Optionen behalten Schlüssel und Position — keine Umsortierung, die Nutzer verwirrt
- Änderungen greifen ohne HA-Neustart, per Reload der Entry

**Neue Entities, die der Nutzer zuordnen muss**
Falls das Kennlinienlernen zusätzliche Messquellen braucht (z. B. eine AC-Leistungs-Entity pro Kraftwerk als Trainingsziel): diese als **Entity-Selector im Options Flow** anbieten, nicht raten und nicht über Namenskonvention erschließen. Kein gesetztes Feld → Lernen für dieses Kraftwerk deaktiviert, mit klarer Meldung statt stillem Nichtstun.

---

## 6. Bereitzustellende Entities

Pro Kraftwerk, jeweils `today` und `tomorrow`:

- `sensor.pvstrings_<plant>_forecast_dc_*` — **bestehend, unverändert** (ID, Einheit, Semantik)
- `sensor.pvstrings_<plant>_forecast_ac_*` — neu, nur `direct`
- `sensor.pvstrings_<plant>_forecast_battery_charge_*` — neu, nur `storage`
- Summensensor AC über alle `direct`-Kraftwerke — Referenzgröße für externe Konsumenten

Anforderungen:
- **Stabile Entity-IDs.** Externe Referenzen hängen daran. Umbenennungen sind Breaking Changes → Rücksprache.
- Neue Entities erscheinen bei bestehenden Installationen erst, wenn der Nutzer den Pfadtyp konfiguriert hat. Solange neutral konfiguriert: AC-Sensor liefert denselben Wert wie DC (Wirkungsgrad 1.0), keine Sprünge in der Historie.
- Attribute transportieren die Semantik: Pfadtyp, verwendete Kennlinie (Datenblatt vs. gelernt), ob Prognose-Clipping wirksam war
- `entity_category: diagnostic` für reine Innensicht
- Definiertes Verhalten ohne Prognose (`unknown` vs. `unavailable`) — konsistent mit dem bisherigen Verhalten der DC-Sensoren

---

## 7. Umsetzungsplan

### Phase 0 — Bestandsaufnahme
- Aktuelle Entity-IDs, Attribute und Persistenzformate dokumentieren, **bevor** etwas geändert wird
- Trainingsdatenformat und Speicherort erfassen, Sicherungsstrategie festlegen (Constraint 0.2)
- Migrationspfad der Config Entry entwerfen und zur Freigabe vorlegen

### Phase 1 — Datenmodell & Migration
- `output_path`, `conversion`-, `storage`-Blöcke
- `async_migrate_entry` mit neutralen Defaults
- Test: bestehende Installation updaten → identisches Verhalten, keine neuen Entities, Trainingsdaten intakt

### Phase 2 — Conversion-Engine
- Reine Funktion: DC-Zeitreihe + Kraftwerkskonfiguration → AC- bzw. Akku-Zeitreihe
- Kennlinien-Loader inkl. Datenblatt-Defaults, lineare Interpolation
- AC-seitiges Clipping am Nennwert
- Unit-Tests: 0 W, unterhalb Anlaufschwelle, Stützstellen, über Nennleistung, Nachtstunden mit Eigenverbrauch, neutrale Konfiguration (muss DC unverändert durchreichen)

### Phase 3 — Config Flow & Entities
- Options Flow gemäß Abschnitt 5
- Sensoren gemäß Abschnitt 6
- Reload ohne Neustart

### Phase 4 — Lernfähige Kennlinie
- Binning nach Last, Anpassung mit Deckelung, Persistenz, Fallback auf Datenblatt
- Diagnose-Ausgabe: Datenblatt- vs. gelernte Kurve nebeneinander
- Absicherung gegen Sensorausfall und gegen Lernen auf Clipping-Fenstern
- Reset-Funktion

### Phase 5 — ML-Residualkorrektur
- Trainingsziel wechselt von geschätztem DC auf gemessenes AC (`direct`) bzw. gemessene Akkuladung (`storage`)
- **Altdaten behalten und markieren, nicht ersetzen** (Constraint 0.2)
- Physikalisches Vormodell bleibt vorgeschaltet, ML korrigiert nur den Rest
- **Nicht mit Phase 4 vermischen:** Kennlinienlernen ist ein interpretierbarer Parameterfit, ML-Residuum ist Blackbox. Wenn beides gleichzeitig lernt, ist eine Abweichung später nicht zuzuordnen. Phase 4 einfrieren, bevor Phase 5 startet.

### Phase 6 — Validierung
- Mindestens 14 Tage Parallelbetrieb gegen HA-Energie-Dashboard
- MAE und MAPE auf Tagessumme, zusätzlich stundenweise
- Randstunden separat auswerten — dort schlägt die Kennlinie am stärksten durch
- Clipping-Tage separat auswerten

---

## 8. Offene Fragen

1. **Datenblatt-Ausgangswerte HMS-1600-4T.** Bestätigt verbaut ist die HMS-Serie. Bekannter Ankerpunkt: CEC-Spitzenwirkungsgrad 96,70 %. Benötigt wird die **Teillastkurve**, nicht nur der Spitzenwert — falls im Datenblatt nicht vollständig enthalten: Spitzenwert als Anker nehmen, generischen Kurvenverlauf für Mikrowechselrichter mit Trafo ergänzen, klar als Schätzung markieren und Stufe B die Korrektur überlassen. Der häufig zitierte MPPT-Wirkungsgrad von 99,8 % stammt aus der HMS-D-Serie und ist **nicht** ungeprüft zu übernehmen — und wegen 3.2.1 ohnehin nicht als separate Stufe.
2. **Ausgangswerte Speicher.** Literatur: LiFePO4 auf Zellebene rund 95 % je Richtung, Round-Trip 90–95 % unter Laborbedingungen. Realistisch für ein Gesamtsystem niedriger — Feldmessungen an Heimspeichern zeigen Batteriewirkungsgrade von 78 bis 98 %, Systemwirkungsgrade von 79 bis 94 %. Vorschlag: `charge_efficiency` und `discharge_efficiency` je 0,96 als Start (Round-Trip ≈ 0,92), danach aus historischen Lade-/Entladedaten nachziehen. Temperaturabhängigkeit beobachten, vorerst nicht modellieren.
3. **Reicht der pauschale MPPT-Wirkungsgrad für die Victron-Regler?** Nach Phase 6 anhand der Residuen entscheiden. Erst messen, dann verfeinern.
4. **Trainingsdatenlage für Stufe B.** Prüfen, ob gepaarte DC-/AC-Messreihen in ausreichender Auflösung und Zeittiefe vorliegen. Falls nicht: erst Aufzeichnung sicherstellen, Phase 4 verschiebt sich. **Nicht** durch Löschen und Neuaufzeichnen lösen.

---

## 9. Handover an `ha-pvstrings-dashboard`

Das Dashboard ist ein **eigenes Projekt** und wird hier nicht angefasst. PV-Strings liefert Daten; die Anzeigeentscheidung (AC, DC oder beides) trifft die Dashboard-Logik.

**Aufgabe:** Sobald neue Entities existieren, ein **Handover-Dokument für `ha-pvstrings-dashboard`** schreiben. Inhalt mindestens:

- Vollständige Liste der neuen Entities mit exakter ID, Einheit, `device_class`, `state_class`
- Semantik pro Entity — insbesondere `forecast_ac_*` vs. `forecast_battery_charge_*` und warum die nicht ohne Weiteres summiert werden dürfen
- Verfügbare Attribute und ihre Aussage (Pfadtyp, Kennlinienquelle, Clipping-Flag)
- Was sich an bestehenden Entities geändert hat — erwartete Antwort: nichts. Falls doch, explizit hervorheben.
- Unter welchen Konfigurationsbedingungen eine Entity überhaupt existiert (z. B. `battery_charge_*` nur bei `storage`) — das Dashboard muss mit fehlenden Entities umgehen können
- Verfügbarkeitsverhalten ohne Prognose
- Empfehlung, welche Größen sinnvoll nebeneinander darstellbar sind

Nicht Teil des Handovers: konkrete Kartenlayouts oder Designvorgaben.

---

## 10. Definition of Done

- [ ] Update auf bestehende, produktive Installation ändert ohne Nutzeraktion **nichts** am Verhalten
- [ ] Trainingsdaten nach dem Update nachweislich vollständig vorhanden
- [ ] Bestehende Entity-IDs, Einheiten und Semantik unverändert
- [ ] `async_migrate_entry` vorhanden, getestet, idempotent
- [ ] Alle neuen Parameter über Config/Options Flow setzbar, keine YAML-only-Optionen
- [ ] Kontextabhängige Formulare (keine Speicherfelder bei `direct`, kein MPPT-Feld bei integriertem MPPT)
- [ ] Validierung fängt doppelte MPPT-Zählung ab
- [ ] `direct`-AC-Prognose deckt sich über 14 Tage mit dem Energie-Dashboard innerhalb dokumentierter Toleranz
- [ ] `storage`-Prognose gegen gemessene Akku-Ladeenergie validiert
- [ ] Kennlinien als Konfigurationsdateien, nicht im Code
- [ ] Gelernte Kennlinie inspizierbar, persistent, rücksetzbar
- [ ] Prognose-Clipping und Trainingsdaten-Invalidierung sind getrennte Codepfade ohne Rückkopplung
- [ ] AC-Summensensor extern referenzierbar und stabil benannt
- [ ] Handover-Dokument für `ha-pvstrings-dashboard` erstellt
- [ ] README dokumentiert die unterschiedliche Einheiten-Semantik der Kraftwerkstypen
- [ ] Projektkonventionen erfüllt (`start.sh`, `/health`, Logs unter `./logs/`, `.env.example`), soweit zutreffend

---

## 11. Arbeitsweise: Codex als Sparringspartner

Codex bitte als **Reviewer und Sparringspartner** hinzuziehen — nicht als Ausführenden, sondern als zweite Meinung:

- **Vor der Implementierung:** Datenmodell (Abschnitt 4) und Migrationsstrategie (0.1, 0.2, Phase 0/1) gegenlesen lassen. Konkrete Frage: Wo könnte dieses Upgrade unbeabsichtigt brechen, ohne dass ein Test es merkt?
- **Zur Trainingsdaten-Migration:** Explizit reviewen lassen, welche Codepfade Trainingsdaten schreiben oder löschen könnten. Das ist der Punkt mit dem höchsten Schadenspotenzial.
- **Zum Config Flow:** Zweite Meinung zur Migration bestehender Entries und zur Bedienlogik der kontextabhängigen Formulare.
- **Beim Kennlinienmodell:** Binning-Strategie, Deckelung, dünn besetzte Laststützstellen, Schutz gegen Drift durch fehlerhafte Messreihen.
- **Nach Phase 2:** Code-Review der Conversion-Engine, besonders Randfälle (Clipping, Anlaufschwelle, Nachtverbrauch, neutrale Konfiguration).
- **Zwischen Phase 4 und 5:** Prüfen lassen, ob die Trennung Kennlinienlernen / ML-Residuum sauber ist.
- **Beim Handover-Dokument:** Aus Sicht eines Konsumenten gegenlesen lassen, der das PV-Strings-Repo nicht kennt.

Abweichende Einschätzungen **dokumentieren statt still auflösen** — auch wenn ihr am Ende beim ursprünglichen Weg bleibt. Gilt besonders für 8.1 und 8.2, wo die Ausgangswerte das spätere Lernverhalten prägen.

---

## 12. Nicht im Scope

- Änderungen am Merit-Order-/ZERO-Controller (nur Bereitstellung der AC-Größe ist Scope)
- Änderungen am Dashboard-Projekt selbst (nur das Handover-Dokument ist Scope)
- Prognose des zeitlichen Ausspeiseprofils bei `storage`-Kraftwerken
- Preis- oder Tarifoptimierung
- Temperaturmodellierung des Speichers
- Jede Änderung, die einen Breaking Change erfordert — dafür Rücksprache statt Alleingang
