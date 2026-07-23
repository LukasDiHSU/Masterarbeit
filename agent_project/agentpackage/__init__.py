"""Shared package for all three multi-agent robot fleet architectures.

Secrets (e.g. ``OPENAI_API_KEY``) must be provided via the environment or a
local ``.env`` file that is *not* committed to version control. Do not
hardcode API keys in this file.
"""

from __future__ import annotations

import os
from pathlib import Path


def _load_env_file() -> None:
    """Load ``agent_project/.env`` into ``os.environ``.

    Deliberately dependency-free (no ``python-dotenv`` import) so this works
    no matter which interpreter runs the script -- including a plain system
    ``python3`` where the venv was not activated. Existing environment
    variables always take precedence over the file.
    """
    here = Path(__file__).resolve().parent
    for directory in (here, *here.parents):
        candidate = directory / ".env"
        if not candidate.is_file():
            continue
        for raw_line in candidate.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
        break


_load_env_file()

if not os.environ.get("OPENAI_API_KEY"):
    import warnings

    warnings.warn(
        "OPENAI_API_KEY is not set. Export it in your shell or put it in a "
        "local .env file (agent_project/.env) before running any agent.",
        stacklevel=2,
    )
