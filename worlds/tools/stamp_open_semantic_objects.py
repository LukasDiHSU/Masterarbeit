#!/usr/bin/env python3
"""Occupancy maps for Experiment 06 must stay walls-only.

Do not stamp rock/barrel/car/container footprints onto the PGM. Object xyz
come from semantic lidar, not the map. This script is a no-op so an old
habit of running it cannot leak those poses into occupancy.
"""

from __future__ import annotations


def main() -> None:
    print(
        "skip: open occupancy maps stay walls-only "
        "(no object footprints). Use get_occupancy_map for agents."
    )


if __name__ == "__main__":
    main()
