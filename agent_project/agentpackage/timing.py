"""Wall-clock helpers for experiment timing (entry points → system thinks done)."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path


def format_elapsed(seconds: float) -> str:
    """Human-readable elapsed duration."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    rem = seconds - 60 * minutes
    if minutes < 60:
        return f"{minutes}m {rem:.1f}s"
    hours = minutes // 60
    minutes = minutes % 60
    return f"{hours}h {minutes}m {rem:.1f}s"


def record_timing(label: str, elapsed_sec: float, *, extra: str = "") -> None:
    """Append a timing line to the active experiment session if one is running."""
    root = os.environ.get("EXPERIMENT_SESSION_DIR", "").strip()
    if not root:
        return
    path = Path(root) / "timings.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (
            f"{datetime.now().astimezone().isoformat()}  "
            f"{label}  {elapsed_sec:.3f}s  ({format_elapsed(elapsed_sec)})"
        )
        if extra:
            line += f"  {extra}"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
