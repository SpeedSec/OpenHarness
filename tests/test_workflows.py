from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from openharness.commands.registry import CommandContext, create_default_command_registry
from openharness.tools.base import ToolExecutionContext
from openharness.tools.workflow_tool import WorkflowCreateInput, WorkflowCreateTool
from openharness.workflows.manager import get_workflow_manager, reset_workflow_manager
from openharness.workflows.runtime import WorkflowRuntime
from openharness.workflows.store import WorkflowStore


@pytest.fixture(autouse=True)
def _reset_workflow_manager():
    reset_workflow_manager()
    yield
    reset_workflow_manager()


class EchoWorkflowAgentRunner:
    def __init__(self, *, fail_if_called: bool = False) -> None:
        self.calls: list[str] = []
        self.running = 0
        self.max_running = 0
        self.fail_if_called = fail_if_called

    async def run_agent(self, *, prompt: str, cwd: Path, model: str | None, phase: str | None, metadata: dict):
        del cwd, model, metadata
        if self.fail_if_called:
            raise AssertionError("cache hit should avoid live agent execution")
        self.calls.append(f"{phase or '-'}:{prompt}")
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            await asyncio.sleep(0.03)
            return f"OUT[{phase or '-'}]:{prompt}", f"task-{len(self.calls)}", None
        finally:
            self.running -= 1


WORKFLOW_SCRIPT = """
return await workflow("demo-workflow", async () => {
  const items = ["a", "b", "c"];
  const first = await phase("fanout", async () => {
    return await parallel(items, item => agent(`inspect ${item}`, { phase: "fanout", name: `worker-${item}` }));
  });
  const reviewed = await phase("review", async () => {
    return await pipeline(first, [
      item => agent(`review ${item}`, { phase: "review" }),
      item => agent(`finalize ${item}`, { phase: "review" }),
    ]);
  });
  await log(`reviewed ${reviewed.length} items`);
  return reviewed.join("\\n");
});
"""

BARE_WORKFLOW_SCRIPT = """
workflow("bare-workflow", async () => {
  const result = await phase("single", async () => {
    return await agent("inspect bare workflow", { phase: "single", name: "worker" });
  });
  return `bare:${result}`;
});
"""

LABELED_AGENT_PHASE_SCRIPT = """
return workflow("labeled-agent-phase", async () => {
  const results = await parallel(["a", "b"], item => {
    return agent(`inspect ${item}`, { phase: "labeled-review" });
  });
  return results.join(",");
});
"""


@pytest.mark.asyncio
async def test_workflow_runtime_runs_parallel_pipeline_and_records_snapshot(tmp_path: Path):
    store = WorkflowStore(tmp_path / "wf")
    run = store.create_run(cwd=tmp_path, name="demo", script=WORKFLOW_SCRIPT, max_concurrency=4)
    runner = EchoWorkflowAgentRunner()

    result = await WorkflowRuntime(store=store, run=run, agent_runner=runner, model="test-model").run_script()

    assert "finalize OUT[review]" in result
    assert len(runner.calls) == 9
    assert runner.max_running > 1

    saved = store.load_run(run.id)
    assert saved is not None
    assert saved.status == "completed"
    assert saved.agent_count == 9
    assert saved.phases["fanout"].agent_count == 3
    assert saved.phases["review"].agent_count == 6
    assert "reviewed 3 items" in saved.logs


@pytest.mark.asyncio
async def test_workflow_runtime_resume_uses_cached_agent_results(tmp_path: Path):
    store = WorkflowStore(tmp_path / "wf")
    run = store.create_run(cwd=tmp_path, name="demo", script=WORKFLOW_SCRIPT, max_concurrency=4)
    await WorkflowRuntime(
        store=store,
        run=run,
        agent_runner=EchoWorkflowAgentRunner(),
        model="test-model",
    ).run_script()

    resumed = store.clone_run_for_resume(run.id)
    assert resumed is not None
    result = await WorkflowRuntime(
        store=store,
        run=resumed,
        agent_runner=EchoWorkflowAgentRunner(fail_if_called=True),
        model="test-model",
    ).run_script()

    assert "finalize OUT[review]" in result
    saved = store.load_run(run.id)
    assert saved is not None
    assert saved.status == "completed"
    assert saved.cached_count >= 9


@pytest.mark.asyncio
async def test_workflow_runtime_waits_for_bare_workflow_call(tmp_path: Path):
    store = WorkflowStore(tmp_path / "wf")
    run = store.create_run(cwd=tmp_path, name="bare", script=BARE_WORKFLOW_SCRIPT, max_concurrency=2)
    runner = EchoWorkflowAgentRunner()

    result = await WorkflowRuntime(store=store, run=run, agent_runner=runner, model="test-model").run_script()

    assert result == "bare:OUT[single]:inspect bare workflow"
    assert runner.calls == ["single:inspect bare workflow"]

    saved = store.load_run(run.id)
    assert saved is not None
    assert saved.status == "completed"
    assert saved.agent_count == 1
    assert saved.phases["single"].agent_count == 1


@pytest.mark.asyncio
async def test_workflow_runtime_completes_agent_labeled_phases(tmp_path: Path):
    store = WorkflowStore(tmp_path / "wf")
    run = store.create_run(cwd=tmp_path, name="labeled", script=LABELED_AGENT_PHASE_SCRIPT, max_concurrency=2)

    await WorkflowRuntime(
        store=store,
        run=run,
        agent_runner=EchoWorkflowAgentRunner(),
        model="test-model",
    ).run_script()

    saved = store.load_run(run.id)
    assert saved is not None
    assert saved.status == "completed"
    assert saved.phases["labeled-review"].status == "completed"
    assert saved.phases["labeled-review"].agent_count == 2


@pytest.mark.asyncio
async def test_workflow_create_tool_saves_and_runs_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    reset_workflow_manager()
    manager = get_workflow_manager()
    manager.set_agent_runner(EchoWorkflowAgentRunner())

    result = await WorkflowCreateTool().execute(
        WorkflowCreateInput(
            name="Demo Tool Workflow",
            script=WORKFLOW_SCRIPT,
            run_immediately=True,
            wait_for_completion=True,
            max_concurrency=4,
        ),
        ToolExecutionContext(cwd=tmp_path, metadata={"model": "test-model"}),
    )

    assert result.is_error is False
    assert "Saved workflow" in result.output
    assert "completed" in result.output
    assert result.metadata["run_id"]
    assert (tmp_path / ".openharness" / "workflows" / "demo-tool-workflow.js").exists()


@pytest.mark.asyncio
async def test_workflows_slash_command_runs_and_shows_saved_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "data"))
    reset_workflow_manager()
    manager = get_workflow_manager()
    manager.set_agent_runner(EchoWorkflowAgentRunner())
    manager.save_script(cwd=tmp_path, name="demo", script=WORKFLOW_SCRIPT)

    registry = create_default_command_registry()
    context = CommandContext(
        engine=SimpleNamespace(model="test-model"),  # type: ignore[arg-type]
        cwd=str(tmp_path),
    )

    command, args = registry.lookup("/workflows run demo")
    result = await command.handler(args, context)
    assert "Started workflow" in result.message
    run_id = result.message.split()[2].rstrip(":")

    await manager.wait(run_id)
    command, args = registry.lookup(f"/workflows output {run_id}")
    output_result = await command.handler(args, context)
    assert "completed" in output_result.message
    assert "result:" in output_result.message
