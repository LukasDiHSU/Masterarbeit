# Experimentplan

Konkretes Runbook abgeleitet aus `tasks/Zwischenpräsentation.pptx`.
Jedes Koordinationsexperiment wird mit **allen Architekturen** unter identischen
Szenario-/Roboter-/Prompt-Bedingungen durchgeführt.

## Dateien

| Datei | Inhalt |
|-------|--------|
| [00_common.md](00_common.md) | Architekturen, Entry Points, Metriken, Timeouts, Fleet-Notes |
| [01_stations.md](01_stations.md) | Paketlieferung (A/B/C/D) |
| [02_bottleneck.md](02_bottleneck.md) | Engstellen-Durchquerung + Prioritäten |
| [03_cross.md](03_cross.md) | Lieferung + max-2-in-crossing Constraint |
| [04_boxes_a.md](04_boxes_a.md) | NL → Waypoints / sparse Hindernisse |
| [05_boxes_b.md](05_boxes_b.md) | NL → Waypoints / dichtere Korridore |
| [prompts/](prompts/) | Einzeilige Prompts für CLI-Paste (keine Zeilenumbrüche) |
| [run_checklist.md](run_checklist.md) | Per-Trial Checkliste + empfohlene Reihenfolge |
| [results_template.md](results_template.md) | Leere Ergebnis-Grids zum Ausfüllen |
| [extensions.md](extensions.md) | Spätere Varianten (Validator, mehr Agenten, Failures) |
| [runs/](runs/) | Gespeicherte Terminal-Logs (`save_experiment.sh <name>`) |

## Empfohlene Reihenfolge

1. **Boxes_a Easy** — Stack Sanity-Check
2. **Stations Easy → Medium** — Allocation
3. **Bottleneck Easy → Medium** — Priorität / Deadlock
4. **Cross Medium** — Reichster einzelner Vergleich
5. **Boxes_b Medium** — Schwierigere Navigation
6. **Hard Cells** — Nach 6-Roboter Fleet Support
7. **Extensions**

## Architekturen

| Architektur | Launch-Skript | Topologie |
|-------------|---------------|-----------|
| Zentralisiert | `launch_centralized.sh` | Stern (Master + Workers) |
| Konflikt-basiert | `launch_conflict_based.sh` | Mesh (bei Events) |
| HMAS-1 | `launch_hmas1.sh` | Stern → DMAS-Fallback |
| HMAS-2 | `launch_hmas2.sh` | Stern mit Feedback |
| DMAS | `launch_dmas.sh` | Vollständig dezentral |
| AgentNet | `launch_agentnet.sh` | Mesh mit Runden |

## Metriken

Für jeden Experimentlauf werden erfasst:

| Metrik | Quelle |
|--------|--------|
| **Input Tokens** | Usage Monitor |
| **Output Tokens** | Usage Monitor |
| **LLM Calls** | Usage Monitor |
| **Inter-Agenten-Nachrichten** | Usage Monitor |
| **Mission erfolgreich** | Manuell (Ja/Nein + Grund) |
| **Wandzeit** | Stopuhr |

## Experiment speichern

Nach einem Trial:

```bash
cd agent_project
./save_experiment.sh <name> --stop --note "optionale Notiz"
```

Beispiel-Namen: `stations_easy_hmas2_r1`, `bottleneck_medium_agentnet_r3`

Format: `<szenario>_<schwierigkeit>_<architektur>_r<run-nummer>_<status>`

## Szenarien

### 01 Stations (Paketlieferung)

- **Easy**: 2 Roboter, 2 Stationen
- **Medium**: 4 Roboter, 4 Stationen
- **Hard**: 6 Roboter, 4 Stationen (Ressourcen-Konkurrenz)

### 02 Bottleneck (Engstellen)

- **Easy**: 2 Roboter, breiter Durchgang
- **Medium**: 4 Roboter, enger Durchgang
- **Hard**: 6 Roboter, sehr enger Durchgang

### 03 Cross (Kreuzung)

- **Medium**: 4 Roboter, max. 2 gleichzeitig in Kreuzung
- **Hard**: 6 Roboter, max. 2 gleichzeitig

### 04 Boxes A (Sparse Hindernisse)

- **Easy**: Wenige Hindernisse, direkter Pfad möglich
- **Medium**: Mehr Hindernisse, Umwege nötig

### 05 Boxes B (Dense Hindernisse)

- **Medium**: Viele Hindernisse, enge Korridore
- **Hard**: Sehr dichte Hindernisse, minimale Durchgänge

## Ergebnis-Analyse

Nach Abschluss aller Runs:

1. Terminal-Logs aus `runs/<name>/logs/` extrahieren
2. Usage Monitor Endwerte notieren
3. In `results_template.md` eintragen
4. Vergleichstabellen erstellen
