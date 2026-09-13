"""Workflow execution: an explicit seam between "a run exists" and "a run is running".

The rule this module exists to enforce: **a workflow never runs inside an HTTP request**.
A review can take minutes and must survive the response that started it, the browser
closing, and - in the local application - nothing else, because the process *is* the
runtime. So the initial implementation is an in-process asyncio job manager, and it earns
that simplicity by meeting four conditions:

* state is persisted before and after the work, so a restart leaves a truthful record;
* every exception is captured into a terminal state - nothing can disappear;
* cancellation is cooperative and recorded as its own terminal state;
* a reconnecting client replays events from the database rather than depending on memory.

``WorkflowExecutor`` is the boundary that keeps this replaceable: a worker process, a task
queue or a remote execution service can implement the same three methods without the API
layer noticing.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import Protocol

from reviewflow.observability import get_logger
from reviewflow_app.errors import ConflictError, NotFoundError

_log = get_logger("app.executor")

#: A run's work unit: an awaitable that persists its own terminal state.
RunCallable = Callable[[str], Awaitable[None]]


class WorkflowExecutor(Protocol):
    """Submits, cancels and reports on runs."""

    async def submit(self, run_id: str) -> None: ...

    async def cancel(self, run_id: str) -> bool: ...

    def is_running(self, run_id: str) -> bool: ...

    async def shutdown(self) -> None: ...


class InProcessExecutor:
    """Runs workflows as asyncio tasks in this process.

    Bounded deliberately: at most ``max_concurrent`` runs execute at once, and a submission
    beyond that is *rejected* rather than silently queued forever. A user who is told "at
    capacity" can act on it; a run stuck in a queue nobody drains cannot.

    ``on_unhandled`` is the safety net for the one case the run callable cannot cover: if
    it raises *before* it managed to persist a terminal state, the run would otherwise sit
    in ``running`` forever, and a client watching it would wait on a stream that can never
    end. The callback is given that chance to record a truthful failure.
    """

    def __init__(
        self,
        run_callable: RunCallable,
        *,
        max_concurrent: int = 3,
        on_unhandled: Callable[[str, BaseException], Awaitable[None]] | None = None,
    ) -> None:
        self._run_callable = run_callable
        self._max_concurrent = max_concurrent
        self._on_unhandled = on_unhandled
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def is_running(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    def active(self) -> list[str]:
        return [run_id for run_id in self._tasks if self.is_running(run_id)]

    async def submit(self, run_id: str) -> None:
        if self.is_running(run_id):
            raise ConflictError(f"run {run_id} is already executing")
        if len(self.active()) >= self._max_concurrent:
            raise ConflictError(
                f"the executor is at capacity ({self._max_concurrent} concurrent runs); "
                "wait for a run to finish or cancel one"
            )
        task = asyncio.create_task(self._guarded(run_id), name=f"reviewflow-run-{run_id}")
        self._tasks[run_id] = task
        # A finished task must be reaped even if nobody awaits it, or its exception would
        # be reported as "never retrieved" and lost.
        task.add_done_callback(functools.partial(self._reap, run_id))

    def _reap(self, run_id: str, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            _log.info("run task cancelled", extra={"workflow_id": run_id, "role": "app"})
            return
        error = task.exception()
        if error is not None:
            _log.error(
                "run task raised",
                extra={"workflow_id": run_id, "role": "app", "error": f"{type(error).__name__}: {error}"[:400]},
            )

    async def _guarded(self, run_id: str) -> None:
        """Run the workflow, converting any escape into a logged, durable outcome."""
        try:
            await self._run_callable(run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.error(
                "unhandled executor failure",
                extra={"workflow_id": run_id, "role": "app", "error": f"{type(exc).__name__}: {exc}"[:400]},
            )
            if self._on_unhandled is not None:
                try:
                    await self._on_unhandled(run_id, exc)
                except Exception:
                    _log.error(
                        "could not record an unhandled failure",
                        extra={"workflow_id": run_id, "role": "app"},
                    )

    async def cancel(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        if task is None or task.done():
            if task is None:
                raise NotFoundError(f"run {run_id} is not executing in this process")
            return False
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=30)
        except (TimeoutError, asyncio.CancelledError):
            # The run's own cancellation handler persists the terminal state; if it is slow
            # we stop waiting but do not pretend it finished.
            _log.warning("cancel did not settle in time", extra={"workflow_id": run_id, "role": "app"})
        return True

    async def shutdown(self) -> None:
        """Stop everything on process shutdown, recording each cancellation."""
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(self._tasks.values()):
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (TimeoutError, asyncio.CancelledError, Exception):
                continue
