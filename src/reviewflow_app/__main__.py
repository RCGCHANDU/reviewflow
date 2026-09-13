"""``reviewflow-app`` console entry point: serve the web application.

Kept deliberately small. Local development runs the app and the Vite dev server side by
side (the dev server proxies ``/api`` here); a production-style run serves the built
frontend from this process, which is why ``--static-dir`` exists.
"""

from __future__ import annotations

import argparse
import sys

from reviewflow.observability import configure_logging, get_logger

_log = get_logger("app.__main__")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reviewflow-app",
        description="Run the ReviewFlow web application (API + built frontend).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="port to bind (default: 8000)")
    parser.add_argument(
        "--data-root",
        default=None,
        help="where projects, runs and artifacts are stored (default: $REVIEWFLOW_APP_DATA_ROOT or artifacts/app)",
    )
    parser.add_argument(
        "--static-dir",
        default=None,
        help="directory holding the built frontend (default: $REVIEWFLOW_APP_STATIC_DIR or frontend/dist)",
    )
    parser.add_argument("--reload", action="store_true", help="reload on code changes (development)")
    args = parser.parse_args(argv)

    import os

    if args.data_root:
        os.environ["REVIEWFLOW_APP_DATA_ROOT"] = args.data_root
    if args.static_dir:
        os.environ["REVIEWFLOW_APP_STATIC_DIR"] = args.static_dir

    import uvicorn

    configure_logging()
    _log.info(
        "serving reviewflow app",
        extra={"role": "app", "stage": "startup", "host": args.host, "port": args.port},
    )
    uvicorn.run(
        "reviewflow_app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=None,  # keep reviewflow's own structured logging
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
