"""Application configuration for the ReviewFlow web application.

Separate from ``reviewflow.config.Settings`` on purpose. That object configures a
*workflow run* (models, budgets, safety); this one configures the *application* (where
its data lives, what it will accept over HTTP). Keeping them apart is what allows a run's
exact configuration to be snapshotted per run while the app's own settings stay global.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - configuration error path
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class AppSettings(BaseModel):
    """Process-wide application settings, from ``REVIEWFLOW_APP_*``.

    ``data_root`` is the only place the app writes: project sources, per-run workspaces,
    the application database and produced artifacts all live under it. That single root is
    deliberate - it is the one boundary that would move behind object storage in a hosted
    deployment, so nothing else may hard-code a location.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_root: Path = Path("artifacts/app")
    #: Origins allowed to call the API from a browser during development.
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])
    #: Reject uploads larger than this (bytes). 8 MiB is generous for a document.
    max_upload_bytes: int = 8 * 1024 * 1024
    #: Extensions the app accepts as reviewable source material.
    allowed_source_suffixes: list[str] = Field(
        default_factory=lambda: [".md", ".markdown", ".txt", ".rst", ".py", ".json", ".yaml", ".yml", ".csv"]
    )
    #: Whether the deterministic scripted engine may be used. Enabled by default because
    #: it is how the application can be exercised end to end without spending money; a
    #: hosted deployment would set this false.
    allow_scripted_runs: bool = True
    #: Whether live provider runs are permitted.
    allow_live_runs: bool = True
    #: Serve the built frontend from this directory when it exists.
    static_dir: Path | None = None

    # -- derived locations ------------------------------------------------------ #

    @property
    def projects_dir(self) -> Path:
        return self.data_root / "projects"

    @property
    def runs_dir(self) -> Path:
        return self.data_root / "runs"

    @property
    def database_path(self) -> Path:
        return self.data_root / "app.sqlite3"

    def ensure_dirs(self) -> None:
        for path in (self.data_root, self.projects_dir, self.runs_dir):
            path.mkdir(parents=True, exist_ok=True)

    def run_workspace(self, run_id: str) -> Path:
        """The single writable root a run's editor is granted.

        Built from the run id, never from user input, so no request parameter can
        influence where a workflow is allowed to write.
        """
        return self.runs_dir / run_id / "workspace"

    def source_dir(self, project_id: str, source_id: str) -> Path:
        return self.projects_dir / project_id / "sources" / source_id


@lru_cache(maxsize=1)
def get_app_settings() -> AppSettings:
    """Cached settings for the process. Tests construct :class:`AppSettings` directly."""
    return AppSettings(
        data_root=Path(_env("REVIEWFLOW_APP_DATA_ROOT", "artifacts/app")),
        max_upload_bytes=_env_int("REVIEWFLOW_APP_MAX_UPLOAD_BYTES", 8 * 1024 * 1024),
        allow_scripted_runs=_env_bool("REVIEWFLOW_APP_ALLOW_SCRIPTED", True),
        allow_live_runs=_env_bool("REVIEWFLOW_APP_ALLOW_LIVE", True),
        static_dir=Path(_env("REVIEWFLOW_APP_STATIC_DIR", "frontend/dist")),
        cors_origins=[
            origin.strip()
            for origin in _env(
                "REVIEWFLOW_APP_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
            ).split(",")
            if origin.strip()
        ],
        allowed_source_suffixes=[
            suffix.strip().lower()
            for suffix in _env(
                "REVIEWFLOW_APP_SOURCE_SUFFIXES", ".md,.markdown,.txt,.rst,.py,.json,.yaml,.yml,.csv"
            ).split(",")
            if suffix.strip()
        ],
    )
