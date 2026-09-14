# Masterarbeit: Vergleich von Multi-Agenten-Koordinationsarchitekturen für Roboterflotten

Diese Repository erweitert das Multi-Roboter-Agentensystem aus der
[Studienarbeit](https://github.com/LukasDiHSU/Studienarbeit) um einen
systematischen Vergleich verschiedener Multi-Agenten-Koordinationsarchitekturen.
Alle Architekturen sind auf derselben ROS 2 / MCP-Infrastruktur und derselben
LLM-Agenten-Basisklasse implementiert, um einen fairen Vergleich zu ermöglichen.

## Forschungsfrage

> Wie unterscheiden sich verschiedene Multi-Agenten-Koordinationsarchitekturen
> hinsichtlich Token-Verbrauch, Inter-Agenten-Kommunikation und Erfolgsrate
> bei Roboterflotten-Aufgaben unterschiedlicher Komplexität?

## Implementierte Architekturen

| Architektur | Ordner | Topologie | Delegation |
|-------------|--------|-----------|------------|
| **Zentralisiert (CMAS)** | `architectures/centralized` | Stern (Hub-and-Spoke) über zentralen Broker | Nur der Master-Agent |
| **Konflikt-basiert** | `architectures/conflict_based` | Peers arbeiten solo; Mesh-Verhandlung nur bei Konflikten/Events | Peers verhandeln nur bei relevanten Events |
| **HMAS-1** | `architectures/hmas1` | Zentraler Missionsplan → Abstimmung → Ausführung; bei Ablehnung PMAS-Fallback | Planner schlägt einmal vor; Roboter stimmen ab |
| **HMAS-2** | `architectures/hmas2` | Stern mit lokaler Prüfung: Plan → AGREE/DISAGREE → Neuplanung bis Konsens | Nur der zentrale Planner |
| **DMAS** | `architectures/dmas` | Vollständig dezentral; Peers planen und verhandeln gleichberechtigt | Alle Peers gleichberechtigt |
| **AgentNet** | `architectures/agentnet` | Mesh ohne Planner; Roboter einigen sich auf nächste Aktionen, führen aus, treffen sich wieder | Niemand delegiert - gemeinsame Einigung |

## Projektstruktur

```
Masterarbeit/
├── README.md                    # Diese Datei
├── agent_project/               # Hauptimplementierung
│   ├── agentpackage/           # Python-Paket mit Architekturen
│   │   ├── architectures/      # Die 6 Koordinationsarchitekturen
│   │   ├── mcpserver.py        # MCP-Server für ROS 2 Tools
│   │   ├── BaseAgents.py       # LLM-Agent-Basisklasse
│   │   └── config.py           # Konfiguration
│   └── README.md               # Technische Dokumentation
├── tasks/                       # Experimentplanung
│   ├── experiments/            # Experiment-Dokumentation
│   │   ├── 00_common.md        # Gemeinsame Metriken
│   │   ├── 01_stations.md      # Stations-Szenario
│   │   ├── 02_bottleneck.md    # Engstellen-Szenario
│   │   ├── 03_cross.md         # Kreuzungs-Szenario
│   │   ├── 04_boxes_a.md       # Hindernis-Szenario (sparse)
│   │   ├── 05_boxes_b.md       # Hindernis-Szenario (dense)
│   │   └── runs/               # Gespeicherte Experimentläufe
│   └── Zwischenpräsentation.pptx
├── worlds/                      # Gazebo-Welten und Karten
│   ├── extra_maps/             # Szenarien (bottleneck, cross, ...)
│   ├── items/                  # Station/Box-Definitionen
│   ├── maps/                   # Nav2-Karten
│   └── sdfs/                   # SDF-Weltdateien
├── AgentArchitectures.pdf       # Referenz: Architektur-Übersicht
├── AgentNet.pdf                 # Referenz: AgentNet-Paper
└── Scalable Multi-Robot...pdf   # Referenz: Chen et al.
```

## Schnellstart

### Voraussetzungen

- Python 3.10+
- ROS 2 Humble/Iron mit Nav2
- OpenAI API Key (oder kompatibles LLM)

### Installation

```bash
cd agent_project
python -m venv .venv && source .venv/bin/activate
pip install -e .
export OPENAI_API_KEY=sk-...  # oder .env-Datei verwenden
```

### Architektur starten

```bash
# Beispiel: Zentralisierte Architektur mit 4 Robotern
./agentpackage/architectures/centralized/launch_centralized.sh --agents 4

# AgentNet mit 6 Robotern
./agentpackage/architectures/agentnet/launch_agentnet.sh --agents 6
```

Verfügbare Flottengrößen: **2, 4, 6, oder 8** Arbeitsroboter.

## Metriken und Evaluation

Jeder Experimentlauf erfasst:

| Metrik | Beschreibung |
|--------|-------------|
| **LLM Tokens** | Input/Output Tokens über alle Agenten |
| **Inter-Agenten-Nachrichten** | Anzahl der Koordinationsnachrichten |
| **LLM Aufrufe** | Anzahl der LLM-Invocations |
| **Mission erfolgreich** | Ja/Nein + Grund bei Fehlschlag |
| **Zeit** | Wandzeit von Start bis Ende |

Der integrierte **Usage Monitor** (`python -m agentpackage.monitor`) zeigt
diese Metriken live während der Ausführung.

## Experimente

Die Experimente sind in `tasks/experiments/` dokumentiert:

1. **Stations** - Paketlieferung zwischen Stationen A→B→C→D
2. **Bottleneck** - Engstellen-Durchquerung mit Prioritäten
3. **Cross** - Lieferung mit Kreuzungsbeschränkung (max. 2 Roboter)
4. **Boxes A/B** - Navigation mit Hindernissen

Jedes Experiment wird mit allen Architekturen unter identischen Bedingungen durchgeführt.

## Referenzen

- Chen et al. (2023): *Scalable Multi-Robot Collaboration with Large Language Models*. arXiv:2309.15943
- Studienarbeit: [github.com/LukasDiHSU/Studienarbeit](https://github.com/LukasDiHSU/Studienarbeit)

## Weiterführende Dokumentation

- [`agent_project/README.md`](agent_project/README.md) - Technische Details und Setup
- Architektur-spezifische READMEs in `agent_project/agentpackage/architectures/*/README.md`
- [`tasks/experiments/README.md`](tasks/experiments/README.md) - Experiment-Dokumentation
