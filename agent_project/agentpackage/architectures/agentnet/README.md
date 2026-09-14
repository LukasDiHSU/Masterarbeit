# AgentNet-Architektur (DMAS-Variante)

Paper-Referenz (Chen et al., arXiv:2309.15943, Fig. 3a): Vollständig dezentrale
Multi-Roboter-Planung. Es gibt **keinen zentralen Planer**. Die Roboter
wechseln sich ab, bis sie sich auf die nächsten Aktionen einigen, führen
diesen Chunk aus und treffen sich dann mit dem neuen Zustand wieder.

AgentNet ist dieser DMAS-Loop auf dem bestehenden Peer-to-Peer Mesh
(nicht das Yang et al. Router/Executor-Netzwerk).

## Ablauf

```
   user ──► SmallDeliveryRobot_0 (leitet nur die Reihenfolge)
                     │
              Diskussion in Reihenfolge  _0 → _1 → … → _0 → …
                     │
              erster gültiger EXECUTE chunk
                     │
              jeder Roboter führt seine nächsten 1..K Aktionen aus
                     │
              erneutes Treffen ──► bis jeder Roboter FINISHED sagt
```

## Komponenten

| Datei | Beschreibung |
|-------|-------------|
| `protocol.py` | Prompts, Chunk-Größe (`AGENTNET_CHUNK_STEPS`), Mission-Wrapping |
| `session.py` | Der Loop: Diskussion, Validierung, Dispatch, Reconvene |
| `net_agent.py` | Ein Mesh-Knoten; Dialog nutzt LLM, Aktionen sind MCP-Primitiven |
| `task_cli.py` | Human Entry; Mission geht immer zu `SmallDeliveryRobot_0` |

## Transport

Verwendet `MeshNode` aus `../conflict_based/mesh_bus.py`.

## Antwort-Formate (strikt geparst)

### EXECUTE (Aktionen für jeden Roboter)

```
EXECUTE
SmallDeliveryRobot_0: move_to(station_A); pick(station_A)
SmallDeliveryRobot_1: move_to(station_C); wait()
```

### FINISHED (Mission beendet)

```
FINISHED
```

## Chunk-Validierung

Ein Chunk wird abgelehnt, wenn:

- Nicht jeder Roboter eine Zeile hat
- Eine Aktion nicht in der verfügbaren Liste ist
- Zwei Roboter dieselbe Station im gleichen Schritt teilen
- Der erste Schritt nur aus `wait()` besteht

Nach einem abgelehnten EXECUTE bekommt der gleiche Sprecher den Grund
und **einen** Retry.

## Missions-Ende

Die Mission endet **nur** wenn **jeder** Roboter `FINISHED` in derselben
Diskussionsrunde antwortet. Ein einzelner Roboter, der FINISHED sagt, reicht nicht.

## Rolle von Robot 0

`SmallDeliveryRobot_0` leitet nur die Sprechreihenfolge:

- Spricht im gleichen Turn wie alle anderen
- Schlägt keinen privilegierten Plan vor
- Ist kein Planner, nur Moderator

## Konfiguration

| Parameter | Standard | Beschreibung |
|-----------|----------|--------------|
| `AGENTNET_CHUNK_STEPS` | 2 | Maximale Aktionen pro Roboter pro Chunk |

## Starten

```bash
./launch_agentnet.sh --agents 4  # 2, 4, 6, oder 8 Roboter
```

## Token/Kommunikations-Hypothese

### Vorteile

- **Keine Single-Point-of-Failure**: Kein zentraler Planer
- **Inkrementelle Planung**: Kleine Chunks reduzieren Planungsfehler
- **Geteiltes Wissen**: Alle Roboter sehen den aktuellen Zustand

### Nachteile

- **Overhead bei einfachen Missionen**: Volle Diskussion auch bei trivialen Aufgaben
- **Serialisierte Diskussion**: Wartezeit bei vielen Robotern
- **Konsens-Overhead**: Jeder Roboter muss FINISHED sagen

## Vergleich mit anderen Architekturen

| Aspekt | AgentNet | DMAS | Konflikt-basiert |
|--------|----------|------|------------------|
| Planung | Fixe Runden | Kontinuierlich | Bei Events |
| Chunks | Kurz (1-2 Aktionen) | Variabel | Vollständige Aufgaben |
| Konsens | Jede Runde | Bei Konflikten | Bei Events |
| Struktur | Sehr strukturiert | Flexibel | Reaktiv |

## Unterschied zu DMAS

Beide sind dezentral, aber:

| AgentNet | DMAS |
|----------|------|
| Fixe Diskussionsrunden | Kontinuierliche Planung |
| Kurze Chunks → Ausführung → neue Runde | Verhandlung bei Bedarf |
| Sehr strukturiert | Flexibel |

AgentNet ist strukturierter und vorhersagbarer, DMAS ist flexibler
und kann bei wenigen Konflikten effizienter sein.
