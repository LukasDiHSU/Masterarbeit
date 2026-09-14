# Konflikt-basierte Architektur

## Konzept

```
   R1 plant/fährt allein     R2 plant/fährt allein     R3 plant/fährt allein
            │                      │                      │
            └──────── Verhandlung NUR wenn Event sie betrifft ────────┘
                              │
                    CONFLICT / box_missing
                              │
                         R1 ↔ R2 verhandeln
                              │
                         R3 bleibt still
```

## Kernprinzipien

1. **Solo by Default**: Jeder `robot_peer_agent` arbeitet allein mit MCP-Tools
2. **Event-getriggerte Verhandlung**: Peer-Talk nur bei Konflikt-Events
3. **Minimale Kommunikation**: Verhandlung nur zwischen betroffenen Peers

## Komponenten

| Datei | Beschreibung |
|-------|-------------|
| `robot_peer_agent.py` | Peer-Agent mit Solo-Modus und Verhandlungs-Tools |
| `mesh_bus.py` | Peer-to-Peer Mesh-Transport |
| `mission_cli.py` | CLI zum Starten von Missionen |
| `launch_conflict_based.sh` | Startet alle Komponenten |

## Verhandlungs-Tools

| Tool | Beschreibung |
|------|-------------|
| `negotiate_with` | Verhandlung mit einem spezifischen Peer starten |
| `end_negotiation` | Aktive Verhandlung beenden |
| `report_done_and_confirm` | Mission abschließen und Fleet-Konsens prüfen |

## Abschluss-Protokoll

Bevor ein Agent seine Mission beendet:

```python
report_done_and_confirm(summary)
```

- Teilt allen Peers mit, was dieser Roboter getan hat
- Sammelt AGREE/DISAGREE zur Frage "Ist die Fleet-Mission fertig?"
- Agent darf nur bei `all_agree=True` beenden

## Event-Gate

Ein Hintergrund-Poller liest das MCP-Event-Log. Tool-Fehler emittieren Events:

| Event | Trigger |
|-------|---------|
| `nav_abort` | Navigation abgebrochen |
| `box_missing` | Box nicht gefunden |
| `station_occupied` | Station bereits belegt |
| `drive_failed` | Direkte Fahrt fehlgeschlagen |
| `blocking_robot` | Anderer Roboter blockiert |

Wenn ein Event einen Peer betrifft (inkl. `blocking_robot` bei Nav-Fehler),
wird Verhandlung **nur** zu diesem Subset freigeschaltet.

## Mesh-Transport (`mesh_bus.py`)

- Point-to-Point Kanal für event-getriggerte Verhandlungen
- **Nicht** ein always-on Chat-Fabric
- Verbindungen werden bei Bedarf aufgebaut

## Mission-CLI (`mission_cli.py`)

- Mesh-Name: `CLI`
- Prompt eingeben (kein Kommando-Prefix) → wird an **alle** Roboter parallel gesendet
- Roboter verhandeln bei Konflikt-Events
- Müssen `report_done_and_confirm` vor Abschluss aufrufen

## Token/Kommunikations-Hypothese

> Event-getriggerte Koordination sollte weniger Inter-Agenten-Nachrichten
> und LLM-Tokens verbrauchen als rundenbasierte Shared-Pool-Deliberation,
> wenn Konflikte selten sind — während bei Engstellen/fehlenden Boxen
> nur das betroffene Subset verhandelt.

### Erwartete Vorteile

- **Sparse Konflikte**: Wenig Overhead, da meist Solo-Arbeit
- **Frequent Konflikte**: Nur betroffene Peers verhandeln
- **Robustheit**: Kein Single-Point-of-Failure

### Erwartete Nachteile

- **Hohe Konfliktdichte**: Viele parallele Verhandlungen
- **Komplexe Koordination**: Schwieriger bei globalen Constraints

## Starten

```bash
./launch_conflict_based.sh --agents 4  # 2, 4, 6, oder 8 Roboter
```

## Vergleich mit anderen Architekturen

| Aspekt | Konflikt-basiert | Zentralisiert | AgentNet |
|--------|-----------------|---------------|----------|
| Default-Modus | Solo | Warten auf Master | Diskussion |
| Verhandlung | Bei Events | Nie (Master entscheidet) | Jede Runde |
| Overhead (sparse) | Minimal | Hoch | Mittel |
| Overhead (dense) | Variabel | Hoch | Konstant |
