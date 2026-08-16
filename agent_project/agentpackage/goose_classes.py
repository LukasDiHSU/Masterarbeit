"""GOOSE class IDs used by ATB-EDU semantic sensors."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

GOOSE_CLASSES: dict[int, str] = {
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

NAME_TO_ID: dict[str, int] = {name: i for i, name in GOOSE_CLASSES.items()}


def class_name(class_id: int) -> str:
    return GOOSE_CLASSES.get(int(class_id), f"unknown_{class_id}")


def class_id_for_name(name: str) -> int | None:
    key = (name or "").strip().lower().replace(" ", "_")
    if key.isdigit():
        return int(key)
    return NAME_TO_ID.get(key)


_CLASS_COLOR_CANDIDATES = (
    Path("/opt/atb_hsu/share/iosb_sim_to_gazebo_bridge/config/class_colors.json"),
)

# Used when ATB class_colors.json is missing (tour classes + a few others).
_FALLBACK_RGB: dict[int, tuple[int, int, int]] = {
    12: (183, 151, 98),
    40: (55, 33, 1),
    58: (255, 138, 154),
    60: (208, 208, 0),
}


@lru_cache(maxsize=1)
def rgb_to_class_id_map() -> dict[tuple[int, int, int], int]:
    mapping: dict[tuple[int, int, int], int] = {}
    for path in _CLASS_COLOR_CANDIDATES:
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        for key, rgb in raw.items():
            try:
                cid = int(key)
                r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
            except Exception:
                continue
            mapping[(r, g, b)] = cid
        if mapping:
            return mapping
    for cid, rgb in _FALLBACK_RGB.items():
        mapping[rgb] = cid
    return mapping


def class_id_from_rgb(r: int, g: int, b: int) -> int:
    """Map a semantic-camera pixel to a GOOSE class id."""
    if r == g == b and 0 <= r <= 63:
        return int(r)
    if g == 0 and b == 0 and 0 <= r <= 63:
        return int(r)
    return rgb_to_class_id_map().get((int(r), int(g), int(b)), 0)
