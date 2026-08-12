# Experiment Plan

Concrete runbook derived from `tasks/Zwischenpräsentation.pptx`.
Every coordination experiment is run across **all architectures** under identical
scenario / robot / prompt conditions.

## Files

| File | Contents |
|---|---|
| [00_common.md](00_common.md) | Architectures, entry points, metrics, timeouts, fleet notes |
| [01_stations.md](01_stations.md) | Package delivery (A/B/C/D) |
| [02_bottleneck.md](02_bottleneck.md) | Crossing + priorities |
| [03_cross.md](03_cross.md) | Delivery + max-2-in-crossing |
| [04_boxes_a.md](04_boxes_a.md) | NL → waypoints / sparse obstacles |
| [05_boxes_b.md](05_boxes_b.md) | NL → waypoints / denser corridors |
| [prompts/](prompts/) | Single-line prompts for CLI paste (no line breaks) |
| [run_checklist.md](run_checklist.md) | Per-trial checklist + suggested order |
| [results_template.md](results_template.md) | Empty result grids to fill |
| [extensions.md](extensions.md) | Later variants (validator, more agents, failures) |
| [runs/](runs/) | Saved terminal logs (`save_experiment.sh <name>`) |

## Suggested order

1. **Boxes_a Easy** — sanity-check stack
2. **Stations Easy → Medium** — allocation
3. **Bottleneck Easy → Medium** — priority / deadlock
4. **Cross Medium** — richest single comparison
5. **Boxes_b Medium** — harder nav
6. **Hard cells** — after 6-robot fleet support
7. **Extensions**
