"""In-process manager for dynamic workflow runs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from openharness.config.paths import get_project_workflows_dir, get_user_workflows_dir
from openharness.utils.fs import atomic_write_text
from openharness.workflows.runtime import WorkflowAgentRunner, WorkflowRuntime, default_max_concurrency
from openharness.workflows.store import WorkflowStore
from openharness.workflows.types import WorkflowRunRecord


class WorkflowManager:
    """Start, inspect, pause, and resume workflow runs."""

    def __init__(self, store: WorkflowStore | None = None) -> None:
        self.store = store or WorkflowStore()
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._agent_runner: WorkflowAgentRunner | None = None

    def set_agent_runner(self, runner: WorkflowAgentRunner | None) -> None:
        """Override the agent runner, primarily for tests."""
        self._agent_runner = runner

    async def start_inline(
        self,
        *,
        cwd: str | Path,
        name: str,
        script: str,
        description: str = "",
        model: str | None = None,
        wait: bool = False,
        max_concurrency: int | None = None,
        max_agents: int = 1000,
    ) -> WorkflowRunRecord:
        """Create and start an inline workflow script."""
        run = self.store.create_run(
            cwd=cwd,
            name=name,
            script=script,
            description=description,
            max_agents=max_agents,
            max_concurrency=max_concurrency or default_max_concurrency(),
        )
        self._start_runtime(run, model=model, max_concurrency=max_concurrency, max_agents=max_agents)
        if wait:
            await self.wait(run.id)
        latest = self.store.load_run(run.id)
        return latest or run

    async def start_saved(
        self,
        *,
        cwd: str | Path,
        name_or_path: str,
        model: str | None = None,
        wait: bool = False,
        max_concurrency: int | None = None,
        max_agents: int = 1000,
    ) -> WorkflowRunRecord:
        """Start a saved workflow by name or path."""
        resolved = resolve_workflow_script(cwd, name_or_path)
        if resolved is None:
            raise ValueError(f"Workflow not found: {name_or_path}")
        script = resolved.read_text(encoding="utf-8")
        return await self.start_inline(
            cwd=cwd,
            name=resolved.stem,
            script=script,
            description=f"Saved workflow: {resolved}",
            model=model,
            wait=wait,
            max_concurrency=max_concurrency,
            max_agents=max_agents,
        )

    async def resume(
        self,
        *,
        run_id: str,
        model: str | None = None,
        wait: bool = False,
        max_concurrency: int | None = None,
    ) -> WorkflowRunRecord:
        """Resume a paused or failed workflow using its run cache."""
        run = self.store.clone_run_for_resume(run_id)
        if run is None:
            raise ValueError(f"Workflow run not found: {run_id}")
        self._start_runtime(run, model=model, max_concurrency=max_concurrency, max_agents=run.max_agents)
        if wait:
            await self.wait(run.id)
        latest = self.store.load_run(run.id)
        return latest or run

    async def pause(self, run_id: str) -> WorkflowRunRecord:
        """Pause a running workflow by cancelling the runtime task."""
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        run = self.store.load_run(run_id)
        if run is None:
            raise ValueError(f"Workflow run not found: {run_id}")
        if run.status == "running":
            run.status = "paused"
            self.store.write_snapshot(run)
            self.store.append_event(run_id, "run_paused", {})
        return run

    async def stop(self, run_id: str) -> WorkflowRunRecord:
        """Stop a running workflow and mark it killed."""
        await self.pause(run_id)
        run = self.store.load_run(run_id)
        if run is None:
            raise ValueError(f"Workflow run not found: {run_id}")
        run.status = "killed"
        self.store.write_snapshot(run)
        self.store.append_event(run_id, "run_killed", {})
        return run

    async def wait(self, run_id: str) -> WorkflowRunRecord:
        """Wait for a workflow task to finish."""
        task = self._tasks.get(run_id)
        if task is not None:
            try:
                await task
            except Exception:
                pass
        run = self.store.load_run(run_id)
        if run is None:
            raise ValueError(f"Workflow run not found: {run_id}")
        return run

    def get(self, run_id: str) -> WorkflowRunRecord | None:
        """Return one workflow run."""
        return self.store.load_run(run_id)

    def list_runs(self, *, limit: int = 50) -> list[WorkflowRunRecord]:
        """List workflow runs."""
        return self.store.list_runs(limit=limit)

    def save_script(
        self,
        *,
        cwd: str | Path,
        name: str,
        script: str,
        scope: str = "project",
    ) -> Path:
        """Save a workflow script as a reusable command."""
        safe_name = _safe_workflow_name(name)
        target_dir = get_user_workflows_dir() if scope == "user" else get_project_workflows_dir(cwd)
        path = target_dir / f"{safe_name}.js"
        atomic_write_text(path, script.rstrip() + "\n")
        return path

    def save_run_script(
        self,
        *,
        cwd: str | Path,
        run_id: str,
        name: str,
        scope: str = "project",
    ) -> Path:
        """Save an existing run's script."""
        run = self.store.load_run(run_id)
        if run is None:
            raise ValueError(f"Workflow run not found: {run_id}")
        script = Path(run.script_path).read_text(encoding="utf-8")
        return self.save_script(cwd=cwd, name=name, script=script, scope=scope)

    def _start_runtime(
        self,
        run: WorkflowRunRecord,
        *,
        model: str | None,
        max_concurrency: int | None,
        max_agents: int,
    ) -> None:
        runtime = WorkflowRuntime(
            store=self.store,
            run=run,
            model=model,
            agent_runner=self._agent_runner,
            max_concurrency=max_concurrency,
            max_agents=max_agents,
        )
        task = asyncio.create_task(runtime.run_script())
        self._tasks[run.id] = task

        def _cleanup(done: asyncio.Task[Any]) -> None:
            if self._tasks.get(run.id) is done:
                self._tasks.pop(run.id, None)

        task.add_done_callback(_cleanup)


def list_saved_workflows(cwd: str | Path) -> list[Path]:
    """List saved workflow scripts visible to a project."""
    project_dir = get_project_workflows_dir(cwd)
    user_dir = get_user_workflows_dir()
    paths = list(project_dir.glob("*.js")) + list(user_dir.glob("*.js"))
    return sorted(paths, key=lambda path: (path.name, str(path)))


def resolve_workflow_script(cwd: str | Path, name_or_path: str) -> Path | None:
    """Resolve a saved workflow by explicit path or search path name."""
    raw = name_or_path.strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.exists() and path.is_file():
        return path.resolve()
    name = raw[:-3] if raw.endswith(".js") else raw
    safe_name = _safe_workflow_name(name)
    for base in (get_project_workflows_dir(cwd), get_user_workflows_dir()):
        candidate = base / f"{safe_name}.js"
        if candidate.exists():
            return candidate
    return None


def _safe_workflow_name(name: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "workflow"


_DEFAULT_MANAGER: WorkflowManager | None = None


def get_workflow_manager() -> WorkflowManager:
    """Return the process-local workflow manager."""
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        _DEFAULT_MANAGER = WorkflowManager()
    return _DEFAULT_MANAGER


def reset_workflow_manager() -> None:
    """Reset the process-local workflow manager."""
    global _DEFAULT_MANAGER
    _DEFAULT_MANAGER = None
