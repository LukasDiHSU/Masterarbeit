
from __future__ import annotations

import argparse

from .agent_bus import DEFAULT_HOST, DEFAULT_PORT, run_broker


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the central agent message broker (star topology).")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    run_broker(args.host, args.port)


if __name__ == "__main__":
    main()
