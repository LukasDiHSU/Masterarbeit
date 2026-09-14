# Zentralisierte Architektur (Stern / Hub-and-Spoke)

Paper-Referenz: CMAS (Centralized Multi-Agent System) nach Chen et al.

## Topologie

```
                 ┌────────────┐
   user ───────► │   master   │
                 └─────┬──────┘
              ask_robot │  (Nur Master hat Delegations-Tools)
        ┌──────────┬────────┼──────────┐
        ▼          ▼        ▼          ▼
   SmallDeliveryRobot_0  SDR_1  SDR_2  SDR_3      (Nur Worker)
        │          │        │          │
        └──────────┴───┬────┴──────────┘
                        ▼
                 Zentraler Broker (agent_bus.py)
```

## Komponenten

| Komponente | Datei | Beschreibung |
|------------|-------|-------------|
| **Broker** | `agent_bus.py` / `agent_broker.py` | Zentraler TCP-Prozess für Nachrichtenweiterleitung |
| **Master** | `master_agent.py` | Einziger Agent mit Delegations-Tools |
| **Worker** | `robot_agent.py` | Reaktive Roboter ohne Peer-Kommunikation |

## Broker-Funktionsweise

- Jeder Agent registriert sich beim Broker mit seinem Namen
- Der Broker leitet Nachrichten anhand des `to`-Feldes weiter
- Agenten verbinden sich **nie** direkt miteinander

## Master-Agent

Einziger Agent mit Delegations-Tools:

| Tool | Beschreibung |
|------|-------------|
| `ask_robot` | Anfrage an einen bestimmten Roboter |
| `ask_all_robots` | Anfrage an alle Roboter (sequenziell) |
| `ask_selected_robots_parallel` | Parallele Anfrage an ausgewählte Roboter |

Der Master ist der **einzige Koordinationspunkt** und damit auch der
**einzige Single-Point-of-Failure**: Fällt Master oder Broker aus,
kann die Flotte nicht mehr koordiniert werden.

## Worker-Roboter

- Rein reaktiv: Beantworten nur eingehende `agent_request`-Nachrichten
- Antworten an den `from`-Absender der Anfrage
- Haben **keine** Tools zur Peer-Kommunikation
- Haben MCP-Tools für Robotersteuerung (Navigation, Pick/Drop)

## Baseline-Status

Diese Architektur ist die aus der Studienarbeit übernommene Baseline.
Die `conflict_based`, `hmas1` und `hmas2` Architekturen bauen darauf auf.

## Starten

```bash
./launch_centralized.sh --agents 4  # 2, 4, 6, oder 8 Roboter
```

Dies startet:
1. MCP-Server
2. Zentralen Broker
3. N Roboter-Worker
4. 1 Master-Agent
5. Usage Monitor
6. Agent Trace

## Token/Kommunikations-Hypothese

- **Vorteil**: Klare Struktur, minimale Verhandlung
- **Nachteil**: Alle Nachrichten laufen über Master → hoher Token-Verbrauch bei großen Flotten
- **Risiko**: Single-Point-of-Failure bei Master oder Broker

## Vergleich mit anderen Architekturen

| Aspekt | Zentralisiert | HMAS-2 | Konflikt-basiert |
|--------|---------------|--------|------------------|
| Topologie | Stern | Stern + Feedback | Mesh (bei Events) |
| Planungsautorität | Master | Planner (mit Review) | Alle Peers |
| Robustheit | Niedrig (SPOF) | Niedrig (SPOF) | Hoch |
| Token pro Mission | Hoch | Mittel-Hoch | Variabel |
