#!/usr/bin/env python3
"""Live GOOSE classes visible in /q1_camera/labels_map (Gazebo semantic camera)."""

from __future__ import annotations

import argparse
import array
import sys
from collections import Counter

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image

# GOOSE labeling scheme (https://goose-dataset.de)
GOOSE_CLASSES = {
    0: "undefined",
    1: "traffic_cone",
    2: "snow",
    3: "cobble",
    4: "obstacle",
    5: "leaves",
    6: "street_light",
    7: "bikeway",
    8: "ego_vehicle",
    9: "pedestrian_crossing",
    10: "road_block",
    11: "road_marking",
    12: "car",
    13: "bicycle",
    14: "person",
    15: "bus",
    16: "forest",
    17: "bush",
    18: "moss",
    19: "traffic_light",
    20: "motorcycle",
    21: "sidewalk",
    22: "curb",
    23: "asphalt",
    24: "gravel",
    25: "boom_barrier",
    26: "rail_track",
    27: "tree_crown",
    28: "tree_trunk",
    29: "debris",
    30: "crops",
    31: "soil",
    32: "rider",
    33: "animal",
    34: "truck",
    35: "on_rails",
    36: "caravan",
    37: "trailer",
    38: "building",
    39: "wall",
    40: "rock",
    41: "fence",
    42: "guard_rail",
    43: "bridge",
    44: "tunnel",
    45: "pole",
    46: "traffic_sign",
    47: "misc_sign",
    48: "barrier_tape",
    49: "kick_scooter",
    50: "low_grass",
    51: "high_grass",
    52: "scenery_vegetation",
    53: "sky",
    54: "water",
    55: "wire",
    56: "outlier",
    57: "heavy_machinery",
    58: "container",
    59: "hedge",
    60: "barrel",
    61: "pipe",
    62: "tree_root",
    63: "military_vehicle",
}


def labels_from_image(msg: Image) -> list[int]:
    """Decode sensor_msgs/Image into a list of class IDs (one per pixel)."""
    h, w, step = msg.height, msg.width, msg.step
    enc = (msg.encoding or "").lower()
    data = msg.data

    if enc in ("mono8", "8uc1") or (not enc and step == w):
        labels: list[int] = []
        for row in range(h):
            off = row * step
            labels.extend(data[off : off + w])
        return labels

    if enc in ("mono16", "16uc1") or (not enc and step == w * 2):
        u16 = array.array("H")
        u16.frombytes(bytes(data))
        labels = []
        row_vals = step // 2
        for row in range(h):
            start = row * row_vals
            labels.extend(u16[start : start + w])
        return labels

    if enc in ("rgb8", "bgr8"):
        # Best effort: use first channel (Gazebo labels_map is normally mono).
        channel = 0 if enc == "rgb8" else 2
        labels = []
        for row in range(h):
            off = row * step
            for col in range(w):
                labels.append(data[off + col * 3 + channel])
        return labels

    raise ValueError(f"Unsupported image encoding '{msg.encoding}' (step={step}, w={w})")


CLEAR = "\033[H\033[J"  # cursor home + clear screen
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


class LabelsMapWatcher(Node):
    def __init__(self, topic: str, min_pixels: int, hide_undefined: bool) -> None:
        super().__init__("watch_labels_map")
        self._topic = topic
        self._min_pixels = min_pixels
        self._hide_undefined = hide_undefined
        # Quiet ROS logs so they don't break the live display
        self.get_logger().set_level(rclpy.logging.LoggingSeverity.WARN)
        self.create_subscription(Image, topic, self._on_image, 10)
        sys.stdout.write(HIDE_CURSOR)
        sys.stdout.flush()
        self._draw_waiting()

    def _draw_waiting(self) -> None:
        sys.stdout.write(
            f"{CLEAR}labels_map watcher  (Ctrl+C zum Beenden)\n"
            f"Topic: {self._topic}\n\n"
            f"  warte auf Bild...\n"
        )
        sys.stdout.flush()

    def _on_image(self, msg: Image) -> None:
        try:
            labels = labels_from_image(msg)
        except ValueError as exc:
            sys.stdout.write(f"{CLEAR}Fehler: {exc}\n")
            sys.stdout.flush()
            return

        counts = Counter(labels)
        total = max(sum(counts.values()), 1)
        rows = []
        for class_id, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            if n < self._min_pixels:
                continue
            if self._hide_undefined and class_id == 0:
                continue
            name = GOOSE_CLASSES.get(class_id, f"unknown_{class_id}")
            rows.append((class_id, name, n, 100.0 * n / total))

        stamp = f"{msg.header.stamp.sec}.{msg.header.stamp.nanosec:09d}"
        lines = [
            "labels_map watcher  (Ctrl+C zum Beenden)",
            f"Topic: {self._topic}",
            f"Frame: {stamp}   {msg.width}x{msg.height}   encoding={msg.encoding}",
            "",
            f"  {'ID':>3}  {'Klasse':<22}  {'Pixel':>8}  {'Anteil':>7}",
            f"  {'-' * 3}  {'-' * 22}  {'-' * 8}  {'-' * 7}",
        ]
        if not rows:
            lines.append("  (keine Klassen über dem Schwellwert)")
        else:
            for class_id, name, n, pct in rows:
                lines.append(f"  {class_id:3d}  {name:<22}  {n:8d}  {pct:6.2f}%")

        sys.stdout.write(CLEAR + "\n".join(lines) + "\n")
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Zeigt die aktuell in /q1_camera/labels_map sichtbaren GOOSE-Klassen."
    )
    parser.add_argument(
        "--topic",
        default="/q1_camera/labels_map",
        help="Image-Topic mit Label-IDs (default: %(default)s)",
    )
    parser.add_argument(
        "--min-pixels",
        type=int,
        default=50,
        help="Mindestpixel pro Klasse, sonst ignorieren (default: %(default)s)",
    )
    parser.add_argument(
        "--show-undefined",
        action="store_true",
        help="Auch Klasse 0 (undefined) anzeigen",
    )
    args = parser.parse_args(argv)

    rclpy.init()
    node = LabelsMapWatcher(
        topic=args.topic,
        min_pixels=args.min_pixels,
        hide_undefined=not args.show_undefined,
    )
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(SHOW_CURSOR + "\n")
        sys.stdout.flush()
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
