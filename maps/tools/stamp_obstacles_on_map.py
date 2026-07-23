#!/usr/bin/env python3
"""Stamp axis-aligned (optionally yawed) box footprints onto a Nav2 occupancy map.

Reads box poses/sizes from a Gazebo world SDF (static models with <box><size>),
paints occupied cells onto a base PGM, and writes map.yaml + map.pgm.

Usage:
  python3 stamp_obstacles_on_map.py \\
    --base-pgm ../_base/walls_only.pgm \\
    --world ../scenarios/boxes_a/world.sdf \\
    --out-dir ../scenarios/boxes_a
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from xml.etree import ElementTree as ET


MAP_YAML = """\
image: map.pgm
resolution: {resolution:.6f}
origin: [{ox:.6f}, {oy:.6f}, 0.000000]
negate: 0
occupied_thresh: 0.65
free_thresh: 0.196
"""


def parse_pgm(path: Path) -> tuple[bytes, int, int, int, bytearray]:
    raw = path.read_bytes()
    if raw[0:2] != b"P5":
        raise ValueError(f"Not a binary PGM (P5): {path}")
    i = 2

    def next_token(buf: bytes, idx: int) -> tuple[str, int]:
        while idx < len(buf) and buf[idx] in b" \t\n\r":
            idx += 1
        if idx < len(buf) and buf[idx] == ord("#"):
            while idx < len(buf) and buf[idx] != 10:
                idx += 1
            return next_token(buf, idx)
        j = idx
        while j < len(buf) and buf[j] not in b" \t\n\r":
            j += 1
        return buf[idx:j].decode(), j

    w_s, i = next_token(raw, i)
    h_s, i = next_token(raw, i)
    maxv_s, i = next_token(raw, i)
    w, h, maxv = int(w_s), int(h_s), int(maxv_s)
    i += 1  # single whitespace after maxval
    header = raw[:i]
    pixels = bytearray(raw[i : i + w * h])
    if len(pixels) != w * h:
        raise ValueError(f"PGM pixel count mismatch in {path}")
    return header, w, h, maxv, pixels


def write_pgm(path: Path, header: bytes, pixels: bytearray) -> None:
    path.write_bytes(header + bytes(pixels))


def parse_pose(text: str | None) -> tuple[float, float, float, float, float, float]:
    if not text or not text.strip():
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    parts = [float(x) for x in text.split()]
    while len(parts) < 6:
        parts.append(0.0)
    return tuple(parts[:6])  # type: ignore[return-value]


def parse_boxes_from_world(world_path: Path) -> list[dict]:
    """Extract static box obstacles (skip arena walls / includes)."""
    tree = ET.parse(world_path)
    root = tree.getroot()
    boxes: list[dict] = []

    for model in root.iter("model"):
        name = model.get("name") or ""
        if name in {"turtlebot3_arena_walls", "turtlebot3_world"}:
            continue
        # Drive-on station / pad markers are visual only — do not occupy the map.
        if name.startswith(("station_", "pad_", "marker_")):
            continue
        if model.find("include") is not None and model.find("link") is None:
            continue

        pose_el = model.find("pose")
        mx, my, mz, mroll, mpitch, myaw = parse_pose(pose_el.text if pose_el is not None else None)

        for link in model.findall("link"):
            link_pose_el = link.find("pose")
            lx, ly, lz, lroll, lpitch, lyaw = parse_pose(
                link_pose_el.text if link_pose_el is not None else None
            )
            for coll in link.findall("collision"):
                coll_pose_el = coll.find("pose")
                cx, cy, cz, croll, cpitch, cyaw = parse_pose(
                    coll_pose_el.text if coll_pose_el is not None else None
                )
                box_el = coll.find("./geometry/box/size")
                if box_el is None or box_el.text is None:
                    continue
                sx, sy, sz = [float(v) for v in box_el.text.split()[:3]]
                # Flat floor pads (even without station_ prefix) stay free for Nav2.
                if sz < 0.12:
                    continue
                # Compose yaw only (footprint); ignore roll/pitch for 2D stamp.
                yaw = myaw + lyaw + cyaw
                x = mx + math.cos(myaw) * (lx + cx) - math.sin(myaw) * (ly + cy)
                y = my + math.sin(myaw) * (lx + cx) + math.cos(myaw) * (ly + cy)
                boxes.append(
                    {
                        "name": name,
                        "x": x,
                        "y": y,
                        "yaw": yaw,
                        "sx": sx,
                        "sy": sy,
                    }
                )
    return boxes


def stamp_box(
    pixels: bytearray,
    w: int,
    h: int,
    origin_x: float,
    origin_y: float,
    resolution: float,
    x: float,
    y: float,
    yaw: float,
    sx: float,
    sy: float,
    occupied_value: int = 0,
) -> int:
    """Paint an oriented rectangle. Returns number of cells stamped."""
    hx, hy = sx / 2.0, sy / 2.0
    # Corners in local frame, then rotate+translate to world
    corners = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
    c, s = math.cos(yaw), math.sin(yaw)
    world_corners = [(x + c * lx - s * ly, y + s * lx + c * ly) for lx, ly in corners]
    xs = [p[0] for p in world_corners]
    ys = [p[1] for p in world_corners]
    # Pixel AABB with small padding
    pad = resolution
    min_wx, max_wx = min(xs) - pad, max(xs) + pad
    min_wy, max_wy = min(ys) - pad, max(ys) + pad

    def world_to_pixel(wx: float, wy: float) -> tuple[int, int]:
        mx = int((wx - origin_x) / resolution)
        my = int((wy - origin_y) / resolution)
        row = h - my - 1
        return mx, row

    col0, row0 = world_to_pixel(min_wx, max_wy)
    col1, row1 = world_to_pixel(max_wx, min_wy)
    cmin, cmax = max(0, min(col0, col1)), min(w - 1, max(col0, col1))
    rmin, rmax = max(0, min(row0, row1)), min(h - 1, max(row0, row1))

    # Point-in-rotated-rect via local frame
    stamped = 0
    for row in range(rmin, rmax + 1):
        my = h - row - 1
        wy = origin_y + (my + 0.5) * resolution
        for col in range(cmin, cmax + 1):
            wx = origin_x + (col + 0.5) * resolution
            dx, dy = wx - x, wy - y
            lx = c * dx + s * dy
            ly = -s * dx + c * dy
            if abs(lx) <= hx and abs(ly) <= hy:
                idx = row * w + col
                if pixels[idx] != occupied_value:
                    pixels[idx] = occupied_value
                    stamped += 1
    return stamped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-pgm", type=Path, required=True)
    parser.add_argument("--world", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--resolution", type=float, default=0.05)
    parser.add_argument("--origin-x", type=float, default=-10.0)
    parser.add_argument("--origin-y", type=float, default=-10.0)
    args = parser.parse_args()

    header, w, h, _maxv, pixels = parse_pgm(args.base_pgm)
    boxes = parse_boxes_from_world(args.world)
    total = 0
    for b in boxes:
        n = stamp_box(
            pixels,
            w,
            h,
            args.origin_x,
            args.origin_y,
            args.resolution,
            b["x"],
            b["y"],
            b["yaw"],
            b["sx"],
            b["sy"],
        )
        print(f"  {b['name']}: stamped {n} cells at ({b['x']:.2f}, {b['y']:.2f})")
        total += n

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_pgm = args.out_dir / "map.pgm"
    out_yaml = args.out_dir / "map.yaml"
    write_pgm(out_pgm, header, pixels)
    out_yaml.write_text(
        MAP_YAML.format(
            resolution=args.resolution,
            ox=args.origin_x,
            oy=args.origin_y,
        )
    )
    print(f"Wrote {out_pgm} and {out_yaml} ({len(boxes)} boxes, {total} cells)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
