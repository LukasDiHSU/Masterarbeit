# HMAS-1 Architektur (Zentraler Plan → Abstimmung → Ausführung, sonst DMAS)

Paper-Referenz (Chen et al., arXiv:2309.15943, Fig. 3b): HMAS-1 ist die hybride
Variante von **DMAS**. Ein zentrales LLM schlägt einen **vollständigen
natürlichsprachlichen Missionsplan** vor (geordnete `STEP`-Blöcke, z.B. pick,
dann drop). Der **letzte STEP ist immer `FINISHED`**. Jeder Roboter stimmt
**einmal** über den gesamten Plan ab (`AGREE` / `DISAGREE`) **ohne Debatte**.
Einstimmiges `AGREE` führt die Arbeits-STEPs der Reihe nach aus.

## Ablauf

```
   Zentraler Planner → Vollständige Mission (STEP 1 .. k, FINISHED)     [einmal]
        │
        ▼  je ein AGREE / DISAGREE (parallel, kein Huddle)
   alle AGREE ──► STEP 1 ausführen → STEP_OK/FAILED check ──► … ──► FINISHED
        │                         │
        │                         irgendein STEP_FAILED
        DISAGREE / neuer PLAN     │
        ▼                         ▼
   Originalplan verworfen ──► DMAS: Huddle, dann PLAN/AGREE/FINISHED bis fertig
```

## Komponenten

| Datei | Beschreibung |
|-------|-------------|
| `planner_agent.py` | Zentraler Planner (nur Map-Tools) |
| `robot_agent.py` | Roboter mit Abstimmungs- und Ausführungs-LLM |
| `dialogue.py` | Multi-Step PLAN Parser, One-Shot Vote Prompt |
| `session.py` | HMAS-1 Session-Steuerung |
| `launch_hmas1.sh` | Startet alle Komponenten |

## Planner-Agent

- Schlägt die Original-Mission **einmal** vor (nur Map-Tools)
- Hängt `FINISHED` an, falls das Modell es vergessen hat
- Nach abgelehntem Plan oder fehlgeschlagenem STEP bleibt er still
- `HMAS1Session` übernimmt dann DMAS Turn-Taking

## Roboter-Agenten

Drei verschiedene LLM-Kontexte:

| Kontext | Verwendung |
|---------|-----------|
| **Discussion LLM** | Original-Abstimmung / DMAS-Talk (Read-Only Map-Tools) |
| **Huddle LLM** | Gesprochene DMAS-Turns |
| **Executor LLM** | Dispatched Work STEP + folgende STEP_OK/FAILED Prüfung |

## Abstimmungs-Protokoll

```
Planner: STEP 1: Robot_0 goes to A, picks up box
         STEP 2: Robot_1 goes to B
         STEP 3: FINISHED

Robot_0: AGREE (oder DISAGREE mit Begründung)
Robot_1: AGREE (oder DISAGREE mit Begründung)
```

### Bei einstimmigem AGREE

1. STEP 1 wird ausgeführt
2. Jeder Executor, der sich bewegt hat, meldet `STEP_OK` oder `STEP_FAILED`
3. Bei `STEP_FAILED`: Plan verworfen → DMAS
4. Bei alle `STEP_OK`: Weiter mit STEP 2
5. `FINISHED` STEP beendet die Mission

### Bei DISAGREE oder neuem PLAN

- Originalplan wird verworfen
- Flotte wechselt zu **DMAS-Modus** (Peer-Netzwerk)
- Planner ist nun still
- Peers verhandeln gleichberechtigt

## Limits

| Parameter | Wert | Beschreibung |
|-----------|------|--------------|
| `PAPER_MAX_PLAN_STEPS` | 12 | Maximale ausgeführte Arbeits-STEPs |
| `PAPER_MAX_SYNTAX_RETRIES` | 3 | Syntax-Wiederholungen |

Limits erreichen → DMAS-Fallback. DMAS-Limit erreichen → Mission als Fehlschlag beenden.

## STEP-Format

Ein Work-STEP ist gewöhnliche Sprache. Innerhalb eines STEPs maximal:
- Eine Fahrt
- Plus maximal ein Pick oder Drop

## Transport

Verwendet `architectures/centralized/agent_bus.py` (nur Stern-Broker).
DMAS-Fallback nutzt das DMAS-Protokoll über diesen Stern, **nicht** ein zweites Mesh.

## Starten

```bash
./launch_hmas1.sh --agents 4  # 2, 4, 6, oder 8 Roboter
```

## Token/Kommunikations-Hypothese

- **Optimaler Fall**: Planner erstellt guten Plan → wenige Tokens, schnelle Ausführung
- **Pessimaler Fall**: Plan wird abgelehnt → DMAS-Overhead zusätzlich zum Planungsversuch
- **Robustheit**: DMAS-Fallback fängt Planner-Fehler ab

## Vergleich mit HMAS-2

| Aspekt | HMAS-1 | HMAS-2 |
|--------|--------|--------|
| Abstimmung | Einmal über ganzen Plan | Pro Zyklus bis Konsens |
| Feedback-Loop | Nein | Ja (AGREE/DISAGREE → Neuplanung) |
| Fallback | DMAS (vollständig dezentral) | Keiner (Planner plant weiter) |
| Roboter-Autonomie | Hoch (bei Fallback) | Niedrig (nur Review) |
