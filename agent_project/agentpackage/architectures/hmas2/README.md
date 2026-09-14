# HMAS-2 Architektur (Zentraler Plan → Lokales Feedback → Ausführung)

Paper-Referenz (Chen et al.): HMAS-2 ist die hybride Variante von **CMAS**.
Ein zentrales LLM schlägt den Flottenplan vor; jeder Roboter hat ein lokales
LLM, das seine zugewiesene Aktion prüft und **AGREE** / **DISAGREE** zurückgibt.
Bei Ablehnung plant der zentrale Agent neu. Erst nach Konsens sendet der
Planner **EXECUTE**-Anweisungen. Roboter reden nie miteinander — der Verkehr
bleibt auf dem zentralisierten Stern-Broker.

## Topologie

```
                         ┌──────────────┐
                  user ► │   planner    │
                         └──────┬───────┘
                collect_feedback │ / ask_* (Broker)
              ┌─────┬─────┴─────┬─────┐
              ▼     ▼           ▼     ▼
            SDR_0 SDR_1       SDR_2  SDR_3
           (Review AGREE/DISAGREE, dann Execute)
```

## Komponenten

| Datei | Beschreibung |
|-------|-------------|
| `planner_agent.py` | Zentraler Planner mit Plan-Draft und Feedback-Collection |
| `robot_agent.py` | Lokaler Reviewer und Executor |
| `launch_hmas2.sh` | Startet alle Komponenten |

## Planner-Agent

### Plan-Phase

1. Entwirft einen Flottenplan
2. Ruft `collect_feedback` auf (sendet `PLAN REVIEW REQUEST`)
3. Sammelt AGREE/DISAGREE von allen Robotern
4. Bei Ablehnung: Überarbeitet Plan und wiederholt

### Execute-Phase

Nach `all_agree`:

```python
ask_robot(robot_id, "EXECUTE: navigate to station_A and pick")
# oder
ask_all_robots("EXECUTE: ...")
# oder
ask_selected_robots(["SDR_0", "SDR_1"], "EXECUTE: ...")
```

## Roboter-Agenten

### Review-Phase

- Empfangen `PLAN REVIEW REQUEST` mit zugewiesener Aktion
- Prüfen mit minimalem Tool-Einsatz (max. `rank_stations_by_distance` einmal)
- Antworten mit:
  - `AGREE: <Begründung>` — Zuweisung akzeptiert
  - `DISAGREE: <Begründung>` — Zuweisung abgelehnt

### Execute-Phase

- Empfangen `EXECUTE`-Anweisung
- Führen Navigation/Hold mit minimalen Tools aus
- **Keine** Event-Tools (`get_events`) — die sind nur für Konflikt-basiert

### Keine Peer-Kommunikation

Roboter haben **keine** Tools zur Peer-Kommunikation.
Alle Nachrichten laufen über den Planner.

## Feedback-Loop

```
Planner: PLAN REVIEW REQUEST
         Robot_0: go to A, pick box
         Robot_1: go to B, wait
         
Robot_0: AGREE: Station A is closest to me
Robot_1: DISAGREE: Station B is blocked

Planner: (überarbeitet Plan)
         PLAN REVIEW REQUEST
         Robot_0: go to A, pick box
         Robot_1: go to C, wait

Robot_0: AGREE
Robot_1: AGREE

Planner: EXECUTE (an alle)
```

## Transport

Verwendet `architectures/centralized/agent_bus.py` + Broker (Stern-Topologie).

## Kontrast zu anderen Architekturen

| Architektur | Besonderheit |
|-------------|-------------|
| **HMAS-2** | Zentraler Plan mit lokalem Review-Loop |
| HMAS-1 | Zentraler Plan; bei Ablehnung PMAS-Peers |
| Zentralisiert/CMAS | Zentraler Plan ohne lokalen Review-Loop |
| DMAS | Kein zentraler Planner |

## Starten

```bash
./launch_hmas2.sh --agents 4  # 2, 4, 6, oder 8 Roboter
```

## Token/Kommunikations-Hypothese

### Vorteile

- **Lokale Validierung**: Roboter können unrealistische Pläne ablehnen
- **Iterative Verbesserung**: Plan wird durch Feedback verbessert
- **Strukturierte Koordination**: Klarer Ablauf ohne Chaos

### Nachteile

- **Overhead bei schlechten Plänen**: Viele Feedback-Loops bei ungeeignetem Initial-Plan
- **Single-Point-of-Failure**: Planner-Ausfall stoppt alles
- **Keine Peer-Hilfe**: Roboter können sich nicht gegenseitig helfen

## Vergleich mit HMAS-1

| Aspekt | HMAS-1 | HMAS-2 |
|--------|--------|--------|
| Feedback-Typ | Einmal über ganzen Plan | Iterativ bis Konsens |
| Bei Ablehnung | DMAS-Fallback (Peers) | Planner plant neu |
| Roboter-Rolle | Abstimmung + ggf. Peer-Planung | Nur Review + Execute |
| Komplexität | Höher (zwei Modi) | Niedriger (ein Modus) |
| Flexibilität | Höher (DMAS-Escape) | Niedriger (Planner-abhängig) |

## Typischer Ablauf

1. **User** gibt Mission ein
2. **Planner** erstellt Initial-Plan
3. **Planner** sendet `PLAN REVIEW REQUEST` an alle Roboter
4. **Roboter** antworten mit AGREE/DISAGREE
5. Bei DISAGREE: Zurück zu Schritt 2
6. Bei alle AGREE: **Planner** sendet `EXECUTE`
7. **Roboter** führen ihre Aktionen aus
8. **Mission beendet**
