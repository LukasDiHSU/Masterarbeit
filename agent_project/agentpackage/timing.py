"""Wall-clock helpers for experiment timing (entry points → system thinks done)."""

from __future__ import annotations

import os
import signal
import threading
import time
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


def experiment_timeout_sec() -> float | None:
    """Seconds from ``EXPERIMENT_TIMEOUT_SEC`` (launch ``--timeout``), or None."""
    raw = os.environ.get("EXPERIMENT_TIMEOUT_SEC", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def stop_experiment_processes() -> None:
    """SIGINT/SIGTERM recorded experiment shells (same idea as save --stop)."""
    root = os.environ.get("EXPERIMENT_SESSION_DIR", "").strip()
    if not root:
        os.kill(os.getpid(), signal.SIGINT)
        return
    pid_path = Path(root) / "pids.txt"
    pids: list[int] = []
    try:
        for token in pid_path.read_text(encoding="utf-8").split():
            if token.isdigit():
                pids.append(int(token))
    except OSError:
        pids = []
    me = os.getpid()
    others = [pid for pid in pids if pid != me]
    for sig in (signal.SIGINT, signal.SIGTERM):
        for pid in others:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                continue
            except PermissionError:
                continue
        time.sleep(0.25)
        still = []
        for pid in others:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            still.append(pid)
        others = still
        if not others:
            break
    os.kill(me, signal.SIGINT)


class mission_timeout_guard:
    """Start the mission clock watchdog when the user prompt is sent.

    If ``EXPERIMENT_TIMEOUT_SEC`` is set and the turn is still running when
    it expires, stop the whole experiment (MCP, robots, CLI).
    Cancelled if the turn finishes first.
    """

    def __init__(self, label: str) -> None:
        self._label = label
        self._limit = experiment_timeout_sec()
        self._timer: threading.Timer | None = None

    def __enter__(self) -> mission_timeout_guard:
        if self._limit is None:
            return self
        self._timer = threading.Timer(self._limit, self._fire)
        self._timer.daemon = True
        self._timer.start()
        print(
            f"--- mission timeout armed: {format_elapsed(self._limit)} "
            f"({self._limit:.0f}s) from this prompt ---",
            flush=True,
        )
        return self

    def __exit__(self, *exc: object) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _fire(self) -> None:
        limit = self._limit or 0.0
        print(
            f"\n*** EXPERIMENT TIMEOUT after {format_elapsed(limit)} "
            f"— stopping all experiment processes ***\n",
            flush=True,
        )
        record_timing(self._label, limit, extra="timeout")
        try:
            root = os.environ.get("EXPERIMENT_SESSION_DIR", "").strip()
            if root:
                Path(root).joinpath("TIMEOUT.txt").write_text(
                    f"Stopped after {format_elapsed(limit)} "
                    f"from the mission prompt ({self._label}).\n",
                    encoding="utf-8",
                )
        except OSError:
            pass
        stop_experiment_processes()
