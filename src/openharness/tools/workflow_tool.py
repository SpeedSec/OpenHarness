"""Tools for creating and running dynamic workflows."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.workflows.manager import get_workflow_manager, list_saved_workflows


WORKFLOW_DSL_HELP = """\
Dynamic workflow scripts are deterministic JavaScript. The runtime injects:
- workflow(name, async () => result): name and run the workflow.
- agent(prompt, opts): run one OpenHarness subagent. opts may include phase, model, name, team, system_prompt, permissions.
- parallel(items, async (item, index) => result): barrier parallel map.
- pipeline(items, [stage1, stage2]): each item independently passes through all stages without a stage barrier.
- phase(name, async () => result): group progress and token accounting.
- log(message): append progress.

The script cannot directly use filesystem, shell, network, Date, Math.random, require, import, process, or fetch.
Agents perform all file/shell work through normal OpenHarness tools and permissions.
Return the final workflow result from workflow(...).
"""


class WorkflowCreateInput(BaseModel):
    """Arguments for creating a reusable workflow."""

    name: str = Field(description="Short workflow name, used as the saved command name")
    script: str = Field(description="Deterministic JavaScript workflow script using workflow/agent/parallel/pipeline/phase/log")
    description: str = Field(default="", description="Short description of what the workflow does")
    scope: str = Field(default="project", description="Save scope: project or user")
    run_immediately: bool = Field(default=True, description="Start a run after saving the workflow")
    wait_for_completion: bool = Field(default=False, description="Wait for the workflow run to finish before returning")
    max_concurrency: int | None = Field(default=None, description="Optional concurrent agent cap, default min(16, cpu-2)")
    max_agents: int = Field(default=1000, description="Hard cap for agent() calls in one run")


class WorkflowRunInput(BaseModel):
    """Arguments for starting a saved workflow."""

    name_or_path: str = Field(description="Saved workflow name or path to a .js workflow script")
    wait_for_completion: bool = Field(default=False, description="Wait for the workflow run to finish before returning")
    max_concurrency: int | None = Field(default=None, description="Optional concurrent agent cap")
    max_agents: int = Field(default=1000, description="Hard cap for agent() calls in one run")


class WorkflowStatusInput(BaseModel):
    """Arguments for inspecting a workflow run."""

    run_id: str = Field(description="Workflow run ID returned by workflow_create or workflow_run")


class WorkflowCreateTool(BaseTool):
    """Create and optionally run a dynamic workflow."""

    name = "workflow_create"
    description = "Create a saved dynamic workflow and optionally run it.\n\n" + WORKFLOW_DSL_HELP
    input_model = WorkflowCreateInput

    async def execute(self, arguments: WorkflowCreateInput, context: ToolExecutionContext) -> ToolResult:
        manager = get_workflow_manager()
        scope = arguments.scope if arguments.scope in {"project", "user"} else "project"
        saved_path = manager.save_script(
            cwd=context.cwd,
            name=arguments.name,
            script=arguments.script,
            scope=scope,
        )
        lines = [f"Saved workflow {arguments.name!r} to {saved_path}"]
        metadata = {"workflow_path": str(saved_path)}
        if arguments.run_immediately:
            run = await manager.start_inline(
                cwd=context.cwd,
                name=arguments.name,
                script=arguments.script,
                description=arguments.description,
                model=_active_model(context),
                wait=arguments.wait_for_completion,
                max_concurrency=arguments.max_concurrency,
                max_agents=arguments.max_agents,
            )
            lines.append(_format_run_summary(run))
            metadata["run_id"] = run.id
            metadata["status"] = run.status
        return ToolResult(output="\n".join(lines), metadata=metadata)


class WorkflowRunTool(BaseTool):
    """Run a saved dynamic workflow."""

    name = "workflow_run"
    description = "Run a saved dynamic workflow by name or .js path.\n\n" + WORKFLOW_DSL_HELP
    input_model = WorkflowRunInput

    async def execute(self, arguments: WorkflowRunInput, context: ToolExecutionContext) -> ToolResult:
        try:
            run = await get_workflow_manager().start_saved(
                cwd=context.cwd,
                name_or_path=arguments.name_or_path,
                model=_active_model(context),
                wait=arguments.wait_for_completion,
                max_concurrency=arguments.max_concurrency,
                max_agents=arguments.max_agents,
            )
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=_format_run_summary(run), metadata={"run_id": run.id, "status": run.status})


class WorkflowStatusTool(BaseTool):
    """Inspect one dynamic workflow run."""

    name = "workflow_status"
    description = "Inspect the status, phases, agents, and final output of a dynamic workflow run."
    input_model = WorkflowStatusInput

    async def execute(self, arguments: WorkflowStatusInput, context: ToolExecutionContext) -> ToolResult:
        del context
        run = get_workflow_manager().get(arguments.run_id)
        if run is None:
            return ToolResult(output=f"Workflow run not found: {arguments.run_id}", is_error=True)
        return ToolResult(output=format_workflow_run(run, include_output=True), metadata={"run_id": run.id, "status": run.status})


def format_workflow_run(run, *, include_output: bool = False) -> str:
    """Render a workflow run for tools and slash commands."""
    lines = [
        _format_run_summary(run),
        f"script: {run.script_path}",
        f"agents: {run.agent_count} live, {run.cached_count} cached",
    ]
    if run.error:
        lines.append(f"error: {run.error}")
    if run.phases:
        lines.append("phases:")
        for phase in run.phases.values():
            lines.append(
                f"- {phase.name}: {phase.status}, agents={phase.agent_count}, cached={phase.cached_count}"
            )
    if run.agents:
        lines.append("agent calls:")
        for agent in run.agents.values():
            suffix = f", task={agent.task_id}" if agent.task_id else ""
            lines.append(f"- {agent.id}: {agent.status}, phase={agent.phase or '-'}{suffix}")
    if run.logs:
        lines.append("logs:")
        for message in run.logs[-10:]:
            lines.append(f"- {message}")
    if include_output and run.result is not None:
        lines.append("result:")
        lines.append(str(run.result))
    return "\n".join(lines)


def list_saved_workflows_text(cwd: str | Path) -> str:
    """Render saved workflows visible from a cwd."""
    paths = list_saved_workflows(cwd)
    if not paths:
        return "No saved workflows."
    return "\n".join(str(path) for path in paths)


def _format_run_summary(run) -> str:
    return f"Workflow {run.id} {run.status}: {run.name}"


def _active_model(context: ToolExecutionContext) -> str | None:
    model = context.metadata.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None
