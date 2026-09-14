# Experiment-Logs (gitignored)

Benannte Speicherstände, erstellt durch `save_experiment.sh`, liegen hier.

Aktive Sessions befinden sich unter `_active/` bis sie gespeichert werden.

## Namenskonvention

```
<szenario>_<schwierigkeit>_<architektur>_r<run-nummer>[_<status>]
```

Beispiele:
- `stations_easy_hmas2_r1`
- `bottleneck_medium_agentnet_r3`
- `cross_hard_centralized_r2_fail_timeout`

## Speichern

```bash
cd agent_project
./save_experiment.sh <name> --stop --note "optionale Notiz"
```

## Inhalt eines Experiment-Ordners

```
<run_name>/
├── logs/
│   ├── usage_monitor.log
│   ├── agent_trace.log
│   ├── mcp_server.log
│   ├── robot_0.log
│   ├── robot_1.log
│   └── ...
├── metadata.txt
└── notes.txt (optional)
```
