"""Isolated backend launcher for the browser end-to-end harness (Phase 7
closeout, finding 5). Run only as a subprocess of
`frontend/tests/e2e/run.mjs` - never invoked directly against a real
database.

**Safety, non-negotiable:** `database.DATABASE_PATH` is redirected to the
caller-supplied, isolated path BEFORE `main` (which calls `ensure_schema()`
at import time) is ever imported - the same discipline every
`verify_*_http.py` script in this project already uses. Both the CLI
argument and the redirected path are asserted to never be named
`shiftops.db`. This process never opens the project's real database.

The frontend's API modules hardcode `http://127.0.0.1:8000` and the real
backend also listens on port 8000 by default - the caller MUST run this on
a different, free port and have the browser layer redirect requests to it
(see run.mjs's route interception). This script never binds port 8000.

Usage (from a subprocess only):
  python e2e_server.py --port <port> --db <path> --origin <isolated frontend origin>
"""

import argparse
import sys
from pathlib import Path


def main_entry():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--db", type=str, required=True)
    parser.add_argument("--origin", type=str, required=True)
    args = parser.parse_args()

    if args.port == 8000:
        raise SystemExit("refusing to bind port 8000 - that is the developer's real backend port")

    db_path = Path(args.db)
    if db_path.name == "shiftops.db":
        raise SystemExit("refusing to run against a file named shiftops.db")

    import database  # noqa: E402  - imported before `main` on purpose

    database.DATABASE_PATH = db_path
    if database.DATABASE_PATH.name == "shiftops.db":  # pragma: no cover - defensive
        raise SystemExit("refusing to run against the project database")

    if "main" in sys.modules:  # pragma: no cover - defensive
        raise SystemExit("main was imported before the database path was redirected; refusing to run.")

    import main  # noqa: E402  - ensure_schema() now runs against the isolated path only
    from fastapi.middleware.cors import CORSMiddleware

    # An ADDITIONAL CORS layer, registered only on THIS isolated process's
    # own `app` object - never a change to main.py's source, so it cannot
    # trigger an autoreloading developer server to restart, and it has no
    # effect on any other process. main.py's own CORSMiddleware (allowing
    # only the developer's real 5173 origin) is untouched and still applies
    # for any request that does not match this one.
    main.app.add_middleware(
        CORSMiddleware,
        allow_origins=[args.origin],
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type"],
    )

    import e2e_fixtures

    connection = database.get_connection()
    try:
        e2e_fixtures.journeys(connection)
    finally:
        connection.close()

    import uvicorn

    uvicorn.run(main.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main_entry()
