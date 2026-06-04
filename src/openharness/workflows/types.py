"""Data models for dynamic workflow runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


WorkflowRunStatus = Literal["pending", "running", "paused", "completed", "failed", "killed"]
WorkflowAgentStatus = Literal["pending", "running", "cached", "completed", "failed", "killed"]
WorkflowPhaseStatus = Literal["pending", "running", "completed", "failed"]


@dataclass
class WorkflowAgentRecord:
    """One subagent invocation inside a workflow run."""

    id: str
    cache_key: str
    prompt: str
    phase: str | None = None
    status: WorkflowAgentStatus = "pending"
    task_id: str | None = None
    output: str | None = None
    error: str | None = None
    model: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    cached: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowPhaseRecord:
    """A named workflow phase shown in progress views."""

    name: str
    status: WorkflowPhaseStatus = "pending"
    started_at: float | None = None
    ended_at: float | None = None
    agent_count: int = 0
    cached_count: int = 0
    token_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowRunRecord:
    """Runtime snapshot for a workflow run."""

    id: str
    name: str
    cwd: str
    script_path: str
    script_hash: str
    status: WorkflowRunStatus = "pending"
    description: str = ""
    result: Any = None
    error: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    ended_at: float | None = None
    agent_count: int = 0
    cached_count: int = 0
    max_agents: int = 1000
    max_concurrency: int = 1
    current_phase: str | None = None
    phases: dict[str, WorkflowPhaseRecord] = field(default_factory=dict)
    agents: dict[str, WorkflowAgentRecord] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
