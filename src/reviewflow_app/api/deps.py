"""Application state and the dependencies the API layer resolves.

One container per process holds the shared, long-lived objects (database, event bus,
services, executor). Route handlers receive it rather than constructing anything, which is
what keeps handlers free of orchestration logic and makes the whole API testable with a
state built over a temporary data root.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from reviewflow_app.config import AppSettings
from reviewflow_app.events import EventBus
from reviewflow_app.execution.executor import InProcessExecutor
from reviewflow_app.persistence.db import Database
from reviewflow_app.persistence.repositories import (
    ArtifactRepository,
    EventRepository,
    ProjectRepository,
    RunRepository,
    SourceRepository,
)
from reviewflow_app.services.providers import ProviderService
from reviewflow_app.services.runs import RunService, SourceService


@dataclass
class AppState:
    """Everything the API needs, wired once."""

    settings: AppSettings
    database: Database
    bus: EventBus = field(default_factory=EventBus)
    #: Filled by :meth:`build`.
    projects: ProjectRepository = field(init=False)
    sources: SourceRepository = field(init=False)
    runs: RunRepository = field(init=False)
    events: EventRepository = field(init=False)
    artifacts: ArtifactRepository = field(init=False)
    provider_service: ProviderService = field(init=False)
    source_service: SourceService = field(init=False)
    run_service: RunService = field(init=False)
    executor: InProcessExecutor = field(init=False)

    def __post_init__(self) -> None:
        self.settings.ensure_dirs()
        self.database.initialise()
        # The repositories are taken from the services rather than constructed a second
        # time: two instances over one database behave identically, which is exactly why a
        # duplicate is dangerous - a test (or a future feature) that patches one silently
        # has no effect on the code path that matters.
        self.provider_service = ProviderService(self.settings)
        self.source_service = SourceService(self.database, self.settings)
        self.run_service = RunService(self.database, self.settings, self.bus)
        self.projects = self.source_service.projects
        self.sources = self.source_service.sources
        self.runs = self.run_service.runs
        self.events = self.run_service.events
        self.artifacts = self.run_service.artifacts
        self.executor = InProcessExecutor(
            self.run_service.execute,
            max_concurrent=3,
            on_unhandled=self.run_service.record_unhandled_failure,
        )

    @classmethod
    def build(cls, settings: AppSettings) -> AppState:
        return cls(settings=settings, database=Database(settings.database_path))

    async def shutdown(self) -> None:
        await self.executor.shutdown()


def get_state(request: Request) -> AppState:
    """FastAPI dependency: the process-wide application state."""
    state: Any = getattr(request.app.state, "app_state", None)
    if state is None:  # pragma: no cover - misconfiguration, not a user error
        raise RuntimeError("application state is not attached to this app")
    return state
