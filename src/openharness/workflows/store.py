"""Persistent storage for dynamic workflow runs."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from hashlib import sha1, sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from openharness.config.paths import get_workflows_dir
from openharness.utils.fs import atomic_write_text
from openharness.workflows.types import (
    WorkflowAgentRecord,
    WorkflowPhaseRecord,
    WorkflowRunRecord,
)


def stable_json(value: Any) -> str:
    """Return deterministic JSON for cache keys."""
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


def workflow_script_hash(script: str) -> str:
    """Return the stable hash for a workflow script."""
    return sha256(script.encode("utf-8")).hexdigest()


def workflow_cache_key(*, script_hash: str, prompt: str, opts: dict[str, Any] | None) -> str:
    """Return the deterministic cache key for one agent call."""
    payload = stable_json(
        {
            "script_hash": script_hash,
            "prompt": prompt,
            "opts": opts or {},
        }
    )
    return sha256(payload.encode("utf-8")).hexdigest()


class WorkflowStore:
    """JSONL journal and snapshot storage for workflow runs."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root is not None else get_workflows_dir()
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def create_run(
        self,
        *,
        cwd: str | Path,
        name: str,
        script: str,
        description: str = "",
        max_agents: int = 1000,
        max_concurrency: int = 1,
    ) -> WorkflowRunRecord:
        """Create a new run directory and initial snapshot."""
        run_id = f"w{uuid4().hex[:10]}"
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "cache").mkdir(exist_ok=True)
        script_path = run_dir / "script.js"
        atomic_write_text(script_path, script.rstrip() + "\n")
        now = time.time()
        record = WorkflowRunRecord(
            id=run_id,
            name=name,
            cwd=str(Path(cwd).resolve()),
            script_path=str(script_path),
            script_hash=workflow_script_hash(script),
            status="pending",
            description=description,
            created_at=now,
            max_agents=max_agents,
            max_concurrency=max_concurrency,
        )
        self.write_snapshot(record)
        self.append_event(run_id, "run_created", asdict(record))
        return record

    def run_dir(self, run_id: str) -> Path:
        """Return a run directory path."""
        return self.runs_dir / run_id

    def snapshot_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "snapshot.json"

    def journal_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "journal.jsonl"

    def cache_path(self, run_id: str, key: str) -> Path:
        return self.run_dir(run_id) / "cache" / f"{key}.json"

    def load_run(self, run_id: str) -> WorkflowRunRecord | None:
        """Load one run snapshot."""
        path = self.snapshot_path(run_id)
        if not path.exists():
            return None
        return self._record_from_payload(json.loads(path.read_text(encoding="utf-8")))

    def list_runs(self, *, limit: int = 50) -> list[WorkflowRunRecord]:
        """List known workflow runs, newest first."""
        records: list[WorkflowRunRecord] = []
        for path in sorted(self.runs_dir.glob("*/snapshot.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                records.append(self._record_from_payload(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            if len(records) >= limit:
                break
        return records

    def write_snapshot(self, record: WorkflowRunRecord) -> None:
        """Persist a workflow run snapshot."""
        run_dir = self.run_dir(record.id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "cache").mkdir(exist_ok=True)
        atomic_write_text(
            self.snapshot_path(record.id),
            json.dumps(asdict(record), indent=2, ensure_ascii=True, default=str) + "\n",
        )

    def append_event(self, run_id: str, event: str, payload: dict[str, Any] | None = None) -> None:
        """Append a journal event."""
        entry = {
            "ts": time.time(),
            "event": event,
            "payload": payload or {},
        }
        with self.journal_path(run_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=True, default=str) + "\n")

    def read_cache(self, run_id: str, key: str) -> Any | None:
        """Read a cached agent result for a run."""
        path = self.cache_path(run_id, key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload.get("result")

    def write_cache(self, run_id: str, key: str, result: Any) -> None:
        """Write a cached agent result for a run."""
        path = self.cache_path(run_id, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"result": result}, ensure_ascii=True, default=str) + "\n")

    def clone_run_for_resume(self, run_id: str) -> WorkflowRunRecord | None:
        """Load a run for resume while preserving its cache directory."""
        record = self.load_run(run_id)
        if record is None:
            return None
        record.status = "pending"
        record.error = None
        record.result = None
        record.started_at = None
        record.ended_at = None
        for agent in record.agents.values():
            if agent.status not in {"completed", "cached"}:
                agent.status = "pending"
                agent.error = None
                agent.ended_at = None
        self.write_snapshot(record)
        self.append_event(run_id, "run_resumed", {})
        return record

    def _record_from_payload(self, payload: dict[str, Any]) -> WorkflowRunRecord:
        phases = {
            name: WorkflowPhaseRecord(**value)
            for name, value in dict(payload.get("phases") or {}).items()
        }
        agents = {
            name: WorkflowAgentRecord(**value)
            for name, value in dict(payload.get("agents") or {}).items()
        }
        payload = dict(payload)
        payload["phases"] = phases
        payload["agents"] = agents
        return WorkflowRunRecord(**payload)


def project_digest(cwd: str | Path) -> str:
    """Return a short stable project digest for future storage partitioning."""
    return sha1(str(Path(cwd).resolve()).encode("utf-8")).hexdigest()[:12]
