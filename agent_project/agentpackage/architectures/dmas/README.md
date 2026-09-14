# DMAS-Architektur (Vollständig Dezentrales Multi-Agenten-System)

Paper-Referenz (Chen et al., arXiv:2309.15943, Fig. 3a): Vollständig dezentrale
Multi-Roboter-Planung. Es gibt **keinen zentralen Planer**. Die Roboter sind
gleichberechtigte Peers, die gemeinsam planen und bei Konflikten verhandeln.

## Topologie

```
   user ──► mission_cli.py
                 │
        Mission an alle Peers parallel
                 │
       ┌─────────┼─────────┐
       ▼         ▼         ▼
    SDR_0 ◄───► SDR_1 ◄───► SDR_2 ◄───► SDR_3
       │         │         │         │
       └─────────┴────┬────┴─────────┘
                      │
              Mesh-Verhandlung bei Bedarf
```

## Komponenten

| Datei | Beschreibung |
|-------|-------------|
| `robot_agent.py` | Peer-Agent mit MCP-Tools und Mesh-Kommunikation |
| `protocol.py` | Verhandlungsprotokoll und Prompts |
| `mission_cli.py` | CLI zum Starten von Missionen |
| `launch_dmas.sh` | Startet alle Komponenten |

## Funktionsweise

1. **Mission-Eingabe**: User gibt Mission über CLI ein
2. **Parallele Verteilung**: CLI sendet Mission an alle Roboter
3. **Dezentrale Planung**: Jeder Roboter plant seinen Teil
4. **Mesh-Verhandlung**: Bei Konflikten verhandeln betroffene Peers
5. **Koordinierte Ausführung**: Roboter führen abgestimmte Aktionen aus
6. **Abschluss-Bestätigung**: `report_done_and_confirm` sammelt AGREE/DISAGREE

## Unterschied zu anderen Architekturen

| Architektur | Planungsautorität | Konfliktlösung |
|-------------|-------------------|----------------|
| **DMAS** | Alle Peers gleichberechtigt | Mesh-Verhandlung |
| Zentralisiert | Nur Master | Master entscheidet |
| HMAS-1 | Planner → bei Ablehnung Peers | Abstimmung → PMAS |
| HMAS-2 | Planner mit Feedback | Feedback-Loop |
| AgentNet | Gemeinsame Einigung | Diskussionsrunden |

## Token/Kommunikations-Hypothese

DMAS sollte weniger zentrale Koordinations-Overhead haben als Stern-Topologien,
aber mehr Verhandlungs-Nachrichten bei häufigen Konflikten generieren.
Die Architektur eignet sich besonders für:

- Räumlich verteilte Aufgaben mit wenig Überlappung
- Szenarien ohne natürliche Hierarchie
- Robustheit gegen Single-Point-of-Failure

## Transport

Verwendet `MeshNode` aus `../conflict_based/mesh_bus.py` für Peer-to-Peer-Kommunikation.

## Starten

```bash
./launch_dmas.sh --agents 4  # 2, 4, 6, oder 8 Roboter
```

## Vergleich mit AgentNet

Beide sind dezentral, aber:

- **DMAS**: Roboter planen kontinuierlich, verhandeln bei Bedarf
- **AgentNet**: Fixe Diskussionsrunden → kurze Chunks → Ausführung → neue Runde

DMAS ist flexibler, AgentNet strukturierter.
