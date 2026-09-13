"""The FastAPI application: middleware, error mapping, static frontend, lifespan."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from reviewflow.observability import configure_logging, get_logger
from reviewflow_app.api.deps import AppState
from reviewflow_app.api.v1 import router as v1_router
from reviewflow_app.config import AppSettings, get_app_settings
from reviewflow_app.errors import AppError
from reviewflow_app.tracing import install_trace_router

_log = get_logger("app.main")


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """Build the application.

    ``settings`` is injectable so tests run against a temporary data root and never touch
    the real project store, the real credential stores or a developer's artifacts.
    """
    effective = settings or get_app_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = AppState.build(effective)
        app.state.app_state = state
        # One process-wide trace router: the SDK has a single global processor list, so a
        # per-run processor would cross-wire concurrent runs' spans.
        install_trace_router()
        _log.info(
            "reviewflow app started",
            extra={"role": "app", "stage": "startup", "data_root": str(effective.data_root)},
        )
        try:
            yield
        finally:
            await state.shutdown()
            _log.info("reviewflow app stopped", extra={"role": "app", "stage": "shutdown"})

    app = FastAPI(
        title="ReviewFlow",
        version="0.1.0",
        description=(
            "A workflow application over the ReviewFlow multi-agent engine: independent "
            "reviewers, one controlled editor, adversarial review and verification."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=effective.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Give every request an id and log its outcome with latency."""
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        started = time.monotonic()
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        _log.info(
            "request",
            extra={
                "role": "app",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "latency_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Give body/query validation failures the same envelope as every other error.

        FastAPI's default shape is a bare list of Pydantic errors, which would make the
        client parse two different error formats. The offending field is named so the user
        is not left guessing which input was rejected.
        """
        errors = exc.errors()
        first = errors[0] if errors else {}
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "body") or "request"
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_failed",
                    "message": f"{location}: {first.get('msg', 'invalid request')}",
                    "retryable": False,
                    "detail": {"errors": jsonable_encoder(errors[:5])},
                }
            },
        )

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        """Deliberate errors carry a code, a retryable flag and safe detail."""
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Never leak a stack trace or a secret; always leave a correlatable log line."""
        _log.error(
            "unhandled error",
            extra={
                "role": "app",
                "path": request.url.path,
                "error": f"{type(exc).__name__}: {exc}"[:500],
            },
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "The request could not be completed. See the server log for the request id.",
                    "retryable": False,
                    "detail": {},
                }
            },
        )

    app.include_router(v1_router)

    static_dir = effective.static_dir
    if static_dir and Path(static_dir).is_dir():
        root = Path(static_dir).resolve()
        # Serving the build needs more than StaticFiles(html=True): that answers index.html
        # only for "/", so a deep link or a page refresh on a client-side route
        # (/runs/<id>) returned the API's JSON 404 - breaking exactly the "reopen a run
        # later" promise the application is built around. Real files come from the static
        # mounts; any other non-API path falls back to the SPA shell, which the client
        # router then resolves. Containment is checked so the fallback cannot read outside
        # the build directory.
        assets = root / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa_fallback(path: str) -> FileResponse:
            if path == "api" or path.startswith("api/"):
                # An unknown API path is a 404, not the application shell: answering a
                # mistyped endpoint with HTML hides the mistake from a client and looks
                # like a success to anything that only checks the status code.
                raise HTTPException(status_code=404, detail=f"no such endpoint: /{path}")
            candidate = (root / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)
            return FileResponse(root / "index.html")

        _log.info("serving built frontend", extra={"role": "app", "static_dir": str(root)})

    return app


configure_logging()

app = create_app()
