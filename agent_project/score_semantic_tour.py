#!/usr/bin/env python3
"""Score an Experiment 06 semantic tour against planted object poses.

Compares the Q1 pose to worlds/items/open.json scoring landmarks.
Does not feed coordinates to agents.

Examples:
  python3 score_semantic_tour.py --tour barrel,rock
  python3 score_semantic_tour.py --tour rock,barrel,car --x 3.4 --y -3.3
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ITEMS = REPO / "worlds" / "items" / "open.json"


def _load_targets() -> dict[str, dict]:
    raw = json.loads(ITEMS.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        if not item.get("scoring_only"):
            continue
        out[str(item["id"])] = item
    return out


def _pose_from_args(x: float | None, y: float | None) -> dict[str, float] | None:
    if x is None or y is None:
        return None
    return {"x": float(x), "y": float(y), "source": "cli"}


_UTM_ORIGIN_X = 458054.71
_UTM_ORIGIN_Y = 5429310.68


def _to_map(x: float, y: float) -> tuple[float, float]:
    if abs(x) > 10000.0 or abs(y) > 10000.0:
        return x - _UTM_ORIGIN_X, y - _UTM_ORIGIN_Y
    return x, y


def _pose_from_ros(topic: str, timeout: float) -> dict[str, float] | None:
    try:
        proc = subprocess.run(
            ["ros2", "topic", "echo", "--once", topic],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    text = proc.stdout or ""
    xs: list[float] = []
    ys: list[float] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("x:"):
            try:
                xs.append(float(line.split(":", 1)[1]))
            except ValueError:
                pass
        elif line.startswith("y:"):
            try:
                ys.append(float(line.split(":", 1)[1]))
            except ValueError:
                pass
    if xs and ys:
        mx, my = _to_map(xs[0], ys[0])
        return {"x": mx, "y": my, "source": topic}
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tour",
        default="barrel,rock",
        help="Comma-separated stop ids in order (default: barrel,rock)",
    )
    parser.add_argument("--x", type=float, default=None)
    parser.add_argument("--y", type=float, default=None)
    parser.add_argument(
        "--pose-topic",
        default="/localization/odometry",
        help="ROS pose topic if --x/--y are omitted",
    )
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args()

    targets = _load_targets()
    stops = [s.strip() for s in args.tour.split(",") if s.strip()]
    missing = [s for s in stops if s not in targets]
    if missing:
        print(f"unknown stops {missing}; known: {sorted(targets)}", file=sys.stderr)
        return 2

    pose = _pose_from_args(args.x, args.y) or _pose_from_ros(args.pose_topic, args.timeout)
    if pose is None:
        print("no pose: pass --x --y or start ROS so the pose topic is live", file=sys.stderr)
        return 3

    print(f"Q1 pose x={pose['x']:.2f} y={pose['y']:.2f} ({pose['source']})")
    reached: list[str] = []
    for stop in stops:
        item = targets[stop]
        dist = math.hypot(pose["x"] - float(item["x"]), pose["y"] - float(item["y"]))
        radius = float(item.get("success_radius_m", 2.5))
        ok = dist <= radius
        flag = "REACHED" if ok else "far"
        print(
            f"  {stop:10s}  class={item.get('class')}  "
            f"target=({item['x']:.1f},{item['y']:.1f})  dist={dist:.2f}m  "
            f"r={radius:.1f}  {flag}"
        )
        if ok:
            reached.append(stop)

    if stops and reached[-1:] == stops[-1:] and set(reached) >= set(stops):
        print("TOUR SUCCESS (currently within radius of every stop; check order in logs)")
        return 0
    if reached:
        print(f"partial: near {reached}")
        return 1
    print("TOUR FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
