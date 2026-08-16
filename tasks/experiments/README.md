# Experiment Plan

Compare five LLM multi-agent architectures on **one Q1** (three sensor
specialists) completing an ordered semantic tour.

## Files

| File | Contents |
|---|---|
| [00_common.md](00_common.md) | Architectures, entry points, metrics, timeouts |
| [06_semantic_tour.md](06_semantic_tour.md) | Q1 semantic tour on the open arena (sense → drive to a centroid) |
| [prompts/](prompts/) | Single-line prompts for CLI paste (no line breaks) |
| [run_checklist.md](run_checklist.md) | Per-trial checklist + suggested order |
| [results_template.md](results_template.md) | Empty result grids to fill |
| [extensions.md](extensions.md) | Later variants |
| [runs/](runs/) | Saved terminal logs (`save_experiment.sh <name>`) |

## Suggested order

1. **Semantic tour Easy, centralized** — smoke-test stack + prompts
2. **Easy on the other four architectures**
3. **Medium, then Hard**, same five architectures
