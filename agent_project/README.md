# agentpackage — Multi-Agenten-Roboterflotten-Architekturen

Dieses Projekt steuert eine Flotte von remroc `SmallDeliveryRobot_0`..`_N-1` Robotern
über ROS 2 / Nav2. Die Roboter werden LLM-Agenten durch einen MCP-Tool-Server
(`agentpackage/mcpserver.py`) zugänglich gemacht. Auf dieser gemeinsamen,
architektur-unabhängigen Werkzeugschicht sind verschiedene Multi-Agenten-
Koordinationsstrategien implementiert, die direkt verglichen werden können.

## Architektur-Übersicht

| Nr. | Architektur | Topologie | Besonderheit |
|-----|-------------|-----------|--------------|
| 1 | **Zentralisiert** | Stern | Ein Master delegiert; alle Nachrichten über Broker |
| 2 | **Konflikt-basiert** | Mesh (bei Events) | Peers arbeiten solo; Verhandlung nur bei Konflikten |
| 3 | **HMAS-1** | Stern → PMAS-Fallback | Zentraler Plan mit Abstimmung; bei Ablehnung Peer-Netzwerk |
| 4 | **HMAS-2** | Stern mit Feedback | Plan → lokale Prüfung → Neuplanung bis Konsens |
| 5 | **DMAS** | Vollständig dezentral | Peers planen und verhandeln gleichberechtigt |
| 6 | **AgentNet** | Mesh | Kurze Aktionschunks → Ausführung → erneute Einigung |

## Flottengrößen

Die Flottengröße `N` wird über `AGENT_COUNT` / `--agents` konfiguriert:
**2, 4, 6, oder 8** Arbeitsroboter (Standard: 4).

Architekturen mit Master/Planner fügen diesen zusätzlich zu den N Arbeitern hinzu.

## Usage Monitor & Agent Trace

Jedes `launch_*.sh`-Skript öffnet zwei Monitoring-Terminals:

### Usage Monitor

Live-Tabelle über alle laufenden Agenten:

```
=== Agent usage monitor === (Ctrl+C to quit)

AGENT                    ARCHITECTURE     MESSAGES  LLM CALLS    IN TOK   OUT TOK  TOTAL TOK
--------------------------------------------------------------------------------------
master                   centralized             4          3       412       128        540
SmallDeliveryRobot_0     centralized             2          2       201        64        265
SmallDeliveryRobot_1     centralized             2          2       198        60        258
...
--------------------------------------------------------------------------------------
TOTAL            5 agent(s)             12          9      1024       340       1364
```

Standalone: `python -m agentpackage.monitor [--host HOST] [--port PORT]`
(Standard: `127.0.0.1:9900`, Umgebungsvariablen: `AGENT_MONITOR_HOST` / `AGENT_MONITOR_PORT`)

### Agent Trace

Scrollendes Log der LLM-Ausgaben und Tool-Aufrufe:

Standalone: `python -m agentpackage.trace_monitor` (Port `9901`, `AGENT_TRACE_MONITOR_PORT`)

### Funktionsweise

- `BaseAgents.py` meldet nach jedem `invoke()` kumulative Token-Nutzung
- Jeder Transport zählt Inter-Agenten-Nachrichten
- UDP-basiert (fire-and-forget): Monitor-Ausfall beeinflusst Agenten nicht
- Kumulative Snapshots: Robustheit bei Paketverlust

## Experimentläufe speichern

Jedes `launch_*.sh` speichert Terminal-Output in `../tasks/experiments/runs/_active/<session>/logs/`.

Nach einem Trial:

```bash
cd agent_project
./save_experiment.sh stations_easy_hmas2_r1 --stop --note "optional note"
```

Dies kopiert die Session nach `tasks/experiments/runs/<run_name>/`.

## Gemeinsame Bausteine

### MCP-Server (`mcpserver.py`)

FastMCP-Server über SSE mit ROS 2-Tools:

| Tool | Beschreibung |
|------|-------------|
| `list_worlds` | Verfügbare Welten auflisten |
| `get_map_info` | Karteninformationen (Stationen, Boxen) |
| `list_robots` | Roboter in der Flotte |
| `get_robot_pose` | Position eines Roboters |
| `get_all_robot_poses` | Positionen aller Roboter |
| `distance_to_station` | Entfernung zu einer Station |
| `rank_stations_by_distance` | Stationen nach Entfernung sortieren |
| `get_peer_distances` | Peer-Entfernungen (nach Nav-Fehler) |
| `drive_distance` | Open-Loop cmd_vel Bewegung |
| `get_laser_snapshot` | Laser-Scan-Daten |
| `navigate_to_pose` | Navigation zu einer Pose |

### MCP-Client (`mcp_client.py`)

Lädt MCP-Tools in LangChain-Agenten.

### BaseAgents (`BaseAgents.py`)

Wrapper um `langchain.agents.create_agent`:
- Per-Agent Checkpointer
- Blockierendes `invoke()`
- REPL (`run_persistent_chat`)

### Konfiguration (`config.py`)

- Modellname
- Fleet-IDs (`SmallDeliveryRobot_*`)
- Deterministische Peer/Port-Tabellen für Mesh

## Installation

```bash
cd agent_project
python -m venv .venv && source .venv/bin/activate
pip install -e .
export OPENAI_API_KEY=sk-...  # Niemals committen; .env-Datei verwenden
```

`OPENAI_API_KEY` muss aus der Umgebung oder einer lokalen, git-ignorierten `.env`-Datei kommen.

## Architekturen starten

Jede Architektur hat ihr eigenes `launch_*.sh`-Skript:

```bash
# Zentralisiert: 1 Broker + N Roboter-Worker + 1 Master
./agentpackage/architectures/centralized/launch_centralized.sh --agents 4

# Konflikt-basiert: N Solo-Peers + Mission-CLI; Verhandlung nur bei Events
./agentpackage/architectures/conflict_based/launch_conflict_based.sh --agents 4

# HMAS-1: 1 Broker + N Roboter + 1 Planner; vollständiger Plan, Abstimmung, PMAS-Fallback
./agentpackage/architectures/hmas1/launch_hmas1.sh --agents 4

# HMAS-2: 1 Broker + N lokale Reviewer + 1 Planner; Plan → Feedback-Loop → Execute
./agentpackage/architectures/hmas2/launch_hmas2.sh --agents 4

# DMAS: N Roboter + Mission-CLI; vollständig dezentrale Planung
./agentpackage/architectures/dmas/launch_dmas.sh --agents 4

# AgentNet: N DMAS-Knoten + Task-CLI; Mission startet bei SmallDeliveryRobot_0
./agentpackage/architectures/agentnet/launch_agentnet.sh --agents 4
```

## Koordinationslogik testen (ohne LLM/ROS 2)

Die Transports (`agent_bus.py`, `mesh_bus.py`) sind reiner Python/Socket-Code:

```python
from agentpackage.architectures.conflict_based.mesh_bus import MeshNode

peers = {"a": ("127.0.0.1", 19101), "b": ("127.0.0.1", 19102)}
a = MeshNode("a", *peers["a"], {"b": peers["b"]})
b = MeshNode("b", *peers["b"], {"a": peers["a"]})
b.on_message(lambda msg: b.reply(msg, f"echo: {msg['text']}") 
             if msg.get("type") == "agent_request" else None)
print(a.ask("b", "ping"))  # -> "echo: ping"
```

## ROS 2 Bring-Up

`launch_tb3_stack.sh` und `commands.txt` starten die Gazebo/Nav2-Welt.

### Szenarien

```bash
SCENARIO=open ./launch_tb3_stack.sh       # Nur Wände
SCENARIO=boxes_a ./launch_tb3_stack.sh    # Sparse Boxen
SCENARIO=boxes_b ./launch_tb3_stack.sh    # Dichte Boxen
SCENARIO=bottleneck ./launch_tb3_stack.sh # Dicke Trennwand, Ein-Roboter-Durchgang
SCENARIO=stations ./launch_tb3_stack.sh   # A→B→C→D am Rand
SCENARIO=cross ./launch_tb3_stack.sh      # Vier Quadranten + Zentrum
SCENARIO=rooms ./launch_tb3_stack.sh      # Vier Eckräume + zentraler Flur
```

### Nav2-Karte nach SDF-Änderung regenerieren

```bash
python3 ../maps/tools/stamp_obstacles_on_map.py \
  --base-pgm ../maps/_base/walls_only.pgm \
  --world ../maps/scenarios/<name>/world.sdf \
  --out-dir ../maps/scenarios/<name>
```

Oder alle Built-in-Layouts neu generieren:

```bash
python3 ../maps/tools/generate_scenarios.py
```

## Architektur-Dokumentation

Siehe die READMEs in den jeweiligen Architektur-Ordnern:

- [`architectures/centralized/README.md`](agentpackage/architectures/centralized/README.md)
- [`architectures/conflict_based/README.md`](agentpackage/architectures/conflict_based/README.md)
- [`architectures/hmas1/README.md`](agentpackage/architectures/hmas1/README.md)
- [`architectures/hmas2/README.md`](agentpackage/architectures/hmas2/README.md)
- [`architectures/dmas/README.md`](agentpackage/architectures/dmas/README.md)
- [`architectures/agentnet/README.md`](agentpackage/architectures/agentnet/README.md)
