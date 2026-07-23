#!/usr/bin/env python3
"""Generate custom TB3 scenario worlds, maps, and items.json files."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

MAPS = Path(__file__).resolve().parents[1]
BASE_PGM = MAPS / "_base" / "walls_only.pgm"
STAMP = MAPS / "tools" / "stamp_obstacles_on_map.py"

WORLD_HEADER = """\
<?xml version="1.0"?>
<sdf version="1.8">
  <world name="default">
    <physics type="ode">
      <real_time_update_rate>1000.0</real_time_update_rate>
      <max_step_size>0.001</max_step_size>
      <real_time_factor>1</real_time_factor>
      <ode>
        <solver>
          <type>quick</type>
          <iters>150</iters>
          <precon_iters>0</precon_iters>
          <sor>1.400000</sor>
          <use_dynamic_moi_rescaling>1</use_dynamic_moi_rescaling>
        </solver>
        <constraints>
          <cfm>0.00001</cfm>
          <erp>0.2</erp>
          <contact_max_correcting_vel>2000.000000</contact_max_correcting_vel>
          <contact_surface_layer>0.01000</contact_surface_layer>
        </constraints>
      </ode>
    </physics>
    <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
    <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>
    <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>
    <plugin filename="gz-sim-sensors-system" name="gz::sim::systems::Sensors">
      <render_engine>ogre2</render_engine>
    </plugin>
    <plugin filename="gz-sim-imu-system" name="gz::sim::systems::Imu"/>

    <include>
      <uri>https://fuel.gazebosim.org/1.0/OpenRobotics/models/Ground Plane</uri>
    </include>
    <include>
      <uri>https://fuel.gazebosim.org/1.0/OpenRobotics/models/Sun</uri>
    </include>

    <model name="turtlebot3_arena_walls">
      <static>1</static>
      <include>
        <uri>model://turtlebot3_arena_walls</uri>
      </include>
    </model>
"""

WORLD_FOOTER = """\
  </world>
</sdf>
"""


def box_model(
    name: str,
    x: float,
    y: float,
    z: float,
    sx: float,
    sy: float,
    sz: float,
    rgb: tuple[float, float, float],
    yaw: float = 0.0,
) -> str:
    r, g, b = rgb
    return f"""
    <model name="{name}">
      <static>true</static>
      <pose>{x:.3f} {y:.3f} {z:.3f} 0 0 {yaw:.4f}</pose>
      <link name="link">
        <collision name="c">
          <geometry><box><size>{sx:.3f} {sy:.3f} {sz:.3f}</size></box></geometry>
        </collision>
        <visual name="v">
          <geometry><box><size>{sx:.3f} {sy:.3f} {sz:.3f}</size></box></geometry>
          <material>
            <ambient>{r:.3f} {g:.3f} {b:.3f} 1</ambient>
            <diffuse>{r:.3f} {g:.3f} {b:.3f} 1</diffuse>
            <specular>0.2 0.2 0.2 1</specular>
          </material>
        </visual>
      </link>
    </model>
"""


def write_scenario(name: str, body: str, items: list[dict]) -> Path:
    out = MAPS / "scenarios" / name
    out.mkdir(parents=True, exist_ok=True)
    (out / "world.sdf").write_text(WORLD_HEADER + body + WORLD_FOOTER)
    (out / "items.json").write_text(json.dumps(items, indent=2) + "\n")
    subprocess.run(
        [
            sys.executable,
            str(STAMP),
            "--base-pgm",
            str(BASE_PGM),
            "--world",
            str(out / "world.sdf"),
            "--out-dir",
            str(out),
        ],
        check=True,
    )
    return out


def fill_free_rect(pgm: Path, x0: float, x1: float, y0: float, y1: float) -> None:
    """Force a rectangle to free (254), used to keep passages open."""
    raw = pgm.read_bytes()
    assert raw[:2] == b"P5"
    i = 2

    def tok(idx: int) -> tuple[str, int]:
        while raw[idx] in b" \t\n\r":
            idx += 1
        if raw[idx] == ord("#"):
            while raw[idx] != 10:
                idx += 1
            return tok(idx)
        j = idx
        while raw[j] not in b" \t\n\r":
            j += 1
        return raw[idx:j].decode(), j

    w_s, i = tok(i)
    h_s, i = tok(i)
    _m, i = tok(i)
    w, h = int(w_s), int(h_s)
    i += 1
    header, pixels = raw[:i], bytearray(raw[i : i + w * h])
    ox, oy, res = -10.0, -10.0, 0.05
    for row in range(h):
        my = h - row - 1
        wy = oy + (my + 0.5) * res
        if wy < y0 or wy > y1:
            continue
        for col in range(w):
            wx = ox + (col + 0.5) * res
            if x0 <= wx <= x1:
                pixels[row * w + col] = 254
    pgm.write_bytes(header + bytes(pixels))


def bottleneck() -> None:
    # Thick bright divider; ~0.45 m gap at y≈0
    body = ""
    body += box_model("divider_north", 0.0, 1.40, 0.4, 0.55, 2.35, 0.8, (0.95, 0.25, 0.1))
    body += box_model("divider_south", 0.0, -1.40, 0.4, 0.55, 2.35, 0.8, (0.95, 0.25, 0.1))
    # Side flanges so the divider is obvious from top-down / RViz
    body += box_model("flange_n_l", -0.45, 0.55, 0.25, 0.35, 0.45, 0.5, (0.9, 0.15, 0.05))
    body += box_model("flange_n_r", 0.45, 0.55, 0.25, 0.35, 0.45, 0.5, (0.9, 0.15, 0.05))
    body += box_model("flange_s_l", -0.45, -0.55, 0.25, 0.35, 0.45, 0.5, (0.9, 0.15, 0.05))
    body += box_model("flange_s_r", 0.45, -0.55, 0.25, 0.35, 0.45, 0.5, (0.9, 0.15, 0.05))
    items = [
        {"id": "station_west", "name": "west station", "x": -1.8, "y": 0.0, "z": 0.0},
        {"id": "station_east", "name": "east station", "x": 1.8, "y": 0.0, "z": 0.0},
        {"id": "gap", "name": "bottleneck gap", "x": 0.0, "y": 0.0, "z": 0.0},
    ]
    out = write_scenario("bottleneck", body, items)
    fill_free_rect(out / "map.pgm", -0.35, 0.35, -0.25, 0.25)


def stations() -> None:
    """Circuit of four stations; center blocked so robots must run the perimeter."""
    body = ""
    # Center block forces perimeter routing A→B→C→D
    body += box_model("center_block", 0.0, 0.0, 0.35, 1.6, 1.6, 0.7, (0.35, 0.35, 0.4))
    # Corner chicanes
    body += box_model("chicane_nw", -1.1, 1.1, 0.25, 0.45, 0.45, 0.5, (0.5, 0.35, 0.2))
    body += box_model("chicane_ne", 1.1, 1.1, 0.25, 0.45, 0.45, 0.5, (0.5, 0.35, 0.2))
    body += box_model("chicane_se", 1.1, -1.1, 0.25, 0.45, 0.45, 0.5, (0.5, 0.35, 0.2))
    body += box_model("chicane_sw", -1.1, -1.1, 0.25, 0.45, 0.45, 0.5, (0.5, 0.35, 0.2))
    # Drive-on station pads (skipped by stamp via station_ prefix / flat height)
    body += box_model("station_a", -1.9, -1.9, 0.03, 0.55, 0.55, 0.06, (0.1, 0.75, 0.2))
    body += box_model("station_b", -1.9, 1.9, 0.03, 0.55, 0.55, 0.06, (0.15, 0.45, 0.9))
    body += box_model("station_c", 1.9, 1.9, 0.03, 0.55, 0.55, 0.06, (0.95, 0.8, 0.1))
    body += box_model("station_d", 1.9, -1.9, 0.03, 0.55, 0.55, 0.06, (0.85, 0.2, 0.55))
    items = [
        {"id": "station_a", "name": "station A", "x": -1.9, "y": -1.9, "z": 0.0},
        {"id": "station_b", "name": "station B", "x": -1.9, "y": 1.9, "z": 0.0},
        {"id": "station_c", "name": "station C", "x": 1.9, "y": 1.9, "z": 0.0},
        {"id": "station_d", "name": "station D", "x": 1.9, "y": -1.9, "z": 0.0},
        {
            "id": "route",
            "name": "suggested route A-B-C-D",
            "x": 0.0,
            "y": 0.0,
            "z": 0.0,
            "note": "Visit stations A→B→C→D along the perimeter; center is blocked.",
        },
    ]
    out = write_scenario("stations", body, items)
    for x, y in [(-1.9, -1.9), (-1.9, 1.9), (1.9, 1.9), (1.9, -1.9)]:
        fill_free_rect(out / "map.pgm", x - 0.35, x + 0.35, y - 0.35, y + 0.35)


def cross() -> None:
    """Plus-shaped walls: four quadrants, meet at center crossing (coordination)."""
    body = ""
    # Arms leave a ~0.5 m wide cross aisle
    body += box_model("arm_n", 0.0, 1.45, 0.35, 0.45, 1.7, 0.7, (0.2, 0.55, 0.75))
    body += box_model("arm_s", 0.0, -1.45, 0.35, 0.45, 1.7, 0.7, (0.2, 0.55, 0.75))
    body += box_model("arm_e", 1.45, 0.0, 0.35, 1.7, 0.45, 0.7, (0.2, 0.55, 0.75))
    body += box_model("arm_w", -1.45, 0.0, 0.35, 1.7, 0.45, 0.7, (0.2, 0.55, 0.75))
    body += box_model("station_nw", -1.8, 1.8, 0.03, 0.5, 0.5, 0.06, (0.1, 0.75, 0.2))
    body += box_model("station_ne", 1.8, 1.8, 0.03, 0.5, 0.5, 0.06, (0.15, 0.45, 0.9))
    body += box_model("station_se", 1.8, -1.8, 0.03, 0.5, 0.5, 0.06, (0.95, 0.8, 0.1))
    body += box_model("station_sw", -1.8, -1.8, 0.03, 0.5, 0.5, 0.06, (0.85, 0.2, 0.55))
    items = [
        {"id": "station_nw", "name": "NW quadrant", "x": -1.8, "y": 1.8, "z": 0.0},
        {"id": "station_ne", "name": "NE quadrant", "x": 1.8, "y": 1.8, "z": 0.0},
        {"id": "station_se", "name": "SE quadrant", "x": 1.8, "y": -1.8, "z": 0.0},
        {"id": "station_sw", "name": "SW quadrant", "x": -1.8, "y": -1.8, "z": 0.0},
        {"id": "crossing", "name": "center crossing", "x": 0.0, "y": 0.0, "z": 0.0},
    ]
    out = write_scenario("cross", body, items)
    # Keep cross aisles free near center
    fill_free_rect(out / "map.pgm", -0.3, 0.3, -0.3, 0.3)
    for x, y in [(-1.8, 1.8), (1.8, 1.8), (1.8, -1.8), (-1.8, -1.8)]:
        fill_free_rect(out / "map.pgm", x - 0.3, x + 0.3, y - 0.3, y + 0.3)


def rooms() -> None:
    """Four corner rooms with doorways; shared central hall."""
    body = ""
    # Room partitions (leave door gaps toward center)
    # NW room walls
    body += box_model("nw_south", -1.5, 0.55, 0.35, 1.4, 0.25, 0.7, (0.55, 0.4, 0.25))
    body += box_model("nw_east", -0.55, 1.5, 0.35, 0.25, 1.4, 0.7, (0.55, 0.4, 0.25))
    # NE
    body += box_model("ne_south", 1.5, 0.55, 0.35, 1.4, 0.25, 0.7, (0.55, 0.4, 0.25))
    body += box_model("ne_west", 0.55, 1.5, 0.35, 0.25, 1.4, 0.7, (0.55, 0.4, 0.25))
    # SE
    body += box_model("se_north", 1.5, -0.55, 0.35, 1.4, 0.25, 0.7, (0.55, 0.4, 0.25))
    body += box_model("se_west", 0.55, -1.5, 0.35, 0.25, 1.4, 0.7, (0.55, 0.4, 0.25))
    # SW
    body += box_model("sw_north", -1.5, -0.55, 0.35, 1.4, 0.25, 0.7, (0.55, 0.4, 0.25))
    body += box_model("sw_east", -0.55, -1.5, 0.35, 0.25, 1.4, 0.7, (0.55, 0.4, 0.25))
    # Station pads in room centers
    body += box_model("station_nw", -1.6, 1.6, 0.03, 0.5, 0.5, 0.06, (0.1, 0.75, 0.2))
    body += box_model("station_ne", 1.6, 1.6, 0.03, 0.5, 0.5, 0.06, (0.15, 0.45, 0.9))
    body += box_model("station_se", 1.6, -1.6, 0.03, 0.5, 0.5, 0.06, (0.95, 0.8, 0.1))
    body += box_model("station_sw", -1.6, -1.6, 0.03, 0.5, 0.5, 0.06, (0.85, 0.2, 0.55))
    items = [
        {"id": "station_nw", "name": "NW room", "x": -1.6, "y": 1.6, "z": 0.0},
        {"id": "station_ne", "name": "NE room", "x": 1.6, "y": 1.6, "z": 0.0},
        {"id": "station_se", "name": "SE room", "x": 1.6, "y": -1.6, "z": 0.0},
        {"id": "station_sw", "name": "SW room", "x": -1.6, "y": -1.6, "z": 0.0},
        {"id": "hall", "name": "central hall", "x": 0.0, "y": 0.0, "z": 0.0},
    ]
    out = write_scenario("rooms", body, items)
    for x, y in [(-1.6, 1.6), (1.6, 1.6), (1.6, -1.6), (-1.6, -1.6)]:
        fill_free_rect(out / "map.pgm", x - 0.3, x + 0.3, y - 0.3, y + 0.3)


def boxes_a_items() -> None:
    """Refresh items.json for existing box scenarios (inside arena)."""
    for name, items in {
        "open": [
            {"id": "center", "name": "arena center", "x": 0.0, "y": 0.0, "z": 0.0},
            {"id": "north", "name": "north point", "x": 0.0, "y": 1.8, "z": 0.0},
            {"id": "south", "name": "south point", "x": 0.0, "y": -1.8, "z": 0.0},
            {"id": "east", "name": "east point", "x": 1.8, "y": 0.0, "z": 0.0},
        ],
        "boxes_a": [
            {"id": "item_1", "name": "north cache", "x": 1.5, "y": 1.5, "z": 0.0},
            {"id": "item_2", "name": "south cache", "x": -1.5, "y": -1.5, "z": 0.0},
            {"id": "item_3", "name": "east cache", "x": 1.8, "y": -0.2, "z": 0.0},
            {"id": "item_4", "name": "collection area", "x": -1.8, "y": 0.2, "z": 0.0},
        ],
        "boxes_b": [
            {"id": "item_1", "name": "NW cache", "x": -1.7, "y": 1.7, "z": 0.0},
            {"id": "item_2", "name": "NE cache", "x": 1.7, "y": 1.7, "z": 0.0},
            {"id": "item_3", "name": "SE cache", "x": 1.7, "y": -1.7, "z": 0.0},
            {"id": "item_4", "name": "collection area", "x": -1.7, "y": -1.7, "z": 0.0},
        ],
    }.items():
        path = MAPS / "scenarios" / name / "items.json"
        path.write_text(json.dumps(items, indent=2) + "\n")
        print(f"wrote {path}")


def main() -> None:
    bottleneck()
    print("bottleneck done")
    stations()
    print("stations done")
    cross()
    print("cross done")
    rooms()
    print("rooms done")
    boxes_a_items()


if __name__ == "__main__":
    main()
