"""Process launcher for the two entrypoints.

Usage::

    python -m app.serve management
    python -m app.serve public

Each container runs exactly two uvicorn worker processes.  The public
entrypoint only reads published projections and has no management routes.
"""

from __future__ import annotations

import os
import sys

import uvicorn

# Both modes always run exactly two uvicorn worker processes.
WORKERS = 2

MODES = {
    "management": ("app.main:app", "ARCHIVE_API_PORT", 8000),
    "public": ("app.public:app", "ARCHIVE_PUBLIC_PORT", 8080),
}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in MODES:
        print("usage: python -m app.serve [management|public]", file=sys.stderr)
        return 2
    target, port_env, default_port = MODES[args[0]]
    port = int(os.environ.get(port_env, default_port))
    uvicorn.run(target, host="0.0.0.0", port=port, workers=WORKERS, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
