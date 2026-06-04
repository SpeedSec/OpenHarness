"""JavaScript-backed dynamic workflow runtime."""

from __future__ import annotations

import asyncio
import json
import os
import textwrap
import time
from pathlib import Path
from typing import Any, Protocol

from openharness.swarm.registry import get_backend_registry
from openharness.swarm.types import TeammateSpawnConfig
from openharness.tasks import get_task_manager
from openharness.workflows.store import WorkflowStore, workflow_cache_key
from openharness.workflows.types import WorkflowAgentRecord, WorkflowPhaseRecord, WorkflowRunRecord


class WorkflowAgentRunner(Protocol):
    """Protocol for executing one workflow subagent call."""

    async def run_agent(
        self,
        *,
        prompt: str,
        cwd: Path,
        model: str | None,
        phase: str | None,
        metadata: dict[str, Any],
    ) -> tuple[str, str | None, str | None]:
        """Return ``(output, task_id, agent_id)``."""


class SubprocessWorkflowAgentRunner:
    """Run workflow subagents through OpenHarness' subprocess swarm backend."""

    async def run_agent(
        self,
        *,
        prompt: str,
        cwd: Path,
        model: str | None,
        phase: str | None,
        metadata: dict[str, Any],
    ) -> tuple[str, str | None, str | None]:
        registry = get_backend_registry()
        executor = registry.get_executor("subprocess")
        name = str(metadata.get("name") or metadata.get("subagent_type") or "workflow-agent")
        team = str(metadata.get("team") or "workflow")
        result = await executor.spawn(
            TeammateSpawnConfig(
                name=name,
                team=team,
                prompt=prompt,
                cwd=str(cwd),
                parent_session_id=str(metadata.get("parent_session_id") or "workflow"),
                model=model,
                system_prompt=metadata.get("system_prompt") if isinstance(metadata.get("system_prompt"), str) else None,
                system_prompt_mode=metadata.get("system_prompt_mode") if metadata.get("system_prompt_mode") in {"append", "replace", "default"} else None,
                permissions=[str(item) for item in metadata.get("permissions", [])] if isinstance(metadata.get("permissions"), list) else [],
                task_type="local_agent",
            )
        )
        if not result.success:
            raise RuntimeError(result.error or "failed to spawn workflow agent")

        manager = get_task_manager()
        while True:
            task = manager.get_task(result.task_id)
            if task is None:
                raise RuntimeError(f"workflow agent task disappeared: {result.task_id}")
            if task.status in {"completed", "failed", "killed"}:
                output = manager.read_task_output(result.task_id, max_bytes=200_000)
                if task.status != "completed":
                    raise RuntimeError(output.strip() or f"workflow agent {result.agent_id} {task.status}")
                return output, result.task_id, result.agent_id
            await asyncio.sleep(0.25)


class WorkflowRuntime:
    """Execute a dynamic workflow script and coordinate subagents."""

    def __init__(
        self,
        *,
        store: WorkflowStore,
        run: WorkflowRunRecord,
        model: str | None = None,
        agent_runner: WorkflowAgentRunner | None = None,
        max_concurrency: int | None = None,
        max_agents: int | None = None,
    ) -> None:
        self.store = store
        self.run = run
        self.model = model
        self.agent_runner = agent_runner or SubprocessWorkflowAgentRunner()
        self.max_concurrency = max_concurrency or run.max_concurrency or default_max_concurrency()
        self.max_agents = max_agents or run.max_agents or 1000
        self._semaphore = asyncio.Semaphore(max(1, self.max_concurrency))
        self._write_lock = asyncio.Lock()
        self._node_process: asyncio.subprocess.Process | None = None
        self._pending_rpc: set[asyncio.Task[None]] = set()

    async def run_script(self) -> Any:
        """Run the workflow script to completion."""
        script_path = Path(self.run.script_path)
        if not script_path.exists():
            raise FileNotFoundError(f"workflow script not found: {script_path}")

        self.run.status = "running"
        self.run.started_at = time.time()
        self.run.ended_at = None
        self.run.error = None
        self.run.max_concurrency = self.max_concurrency
        self.run.max_agents = self.max_agents
        self.store.write_snapshot(self.run)
        self.store.append_event(self.run.id, "run_started", {"max_concurrency": self.max_concurrency})

        driver_path = self.store.run_dir(self.run.id) / "driver.js"
        driver_path.write_text(_NODE_DRIVER, encoding="utf-8")

        try:
            result = await self._run_node(driver_path, script_path)
        except asyncio.CancelledError:
            await self._kill_node()
            self.run.status = "paused"
            self.run.ended_at = time.time()
            self.store.write_snapshot(self.run)
            self.store.append_event(self.run.id, "run_paused", {})
            raise
        except Exception as exc:
            await self._kill_node()
            self._finalize_open_phases("failed")
            self.run.status = "failed"
            self.run.error = str(exc)
            self.run.ended_at = time.time()
            self.store.write_snapshot(self.run)
            self.store.append_event(self.run.id, "run_failed", {"error": str(exc)})
            raise

        self._finalize_open_phases("completed")
        self.run.status = "completed"
        self.run.result = result
        self.run.ended_at = time.time()
        self.store.write_snapshot(self.run)
        self.store.append_event(self.run.id, "run_completed", {"result": result})
        return result

    async def _run_node(self, driver_path: Path, script_path: Path) -> Any:
        self._node_process = await asyncio.create_subprocess_exec(
            "node",
            str(driver_path),
            str(script_path),
            cwd=str(Path(self.run.cwd)),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._node_process.stdout is not None
        assert self._node_process.stderr is not None

        stderr_task = asyncio.create_task(self._drain_stderr(self._node_process.stderr))
        final_seen = False
        final_result: Any = None

        try:
            while True:
                raw = await self._node_process.stdout.readline()
                if not raw:
                    break
                try:
                    message = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"workflow runtime emitted invalid JSON: {raw!r}") from exc

                op = message.get("op")
                if op == "__done":
                    final_seen = True
                    if message.get("ok"):
                        final_result = message.get("result")
                    else:
                        raise RuntimeError(str(message.get("error") or "workflow failed"))
                    continue

                task = asyncio.create_task(self._handle_rpc(message))
                self._pending_rpc.add(task)
                task.add_done_callback(self._pending_rpc.discard)

            if self._pending_rpc:
                await asyncio.gather(*list(self._pending_rpc))
            return_code = await self._node_process.wait()
            await stderr_task
            if return_code != 0:
                raise RuntimeError(f"workflow runtime exited with code {return_code}")
            if not final_seen:
                raise RuntimeError("workflow runtime exited without a result")
            return final_result
        finally:
            stderr_task.cancel()

    async def _handle_rpc(self, message: dict[str, Any]) -> None:
        rpc_id = message.get("id")
        op = message.get("op")
        try:
            if op == "agent":
                result = await self._agent(message)
            elif op == "phase_start":
                result = self._phase_start(str(message.get("name") or "phase"))
            elif op == "phase_end":
                result = self._phase_end(str(message.get("name") or "phase"))
            elif op == "log":
                result = self._log(str(message.get("message") or ""))
            elif op == "workflow_start":
                result = self._workflow_start(str(message.get("name") or self.run.name))
            elif op == "workflow_end":
                result = self._workflow_end(str(message.get("name") or self.run.name), message.get("result"))
            elif op == "workflow_error":
                result = self._workflow_error(str(message.get("name") or self.run.name), str(message.get("error") or ""))
            else:
                raise RuntimeError(f"unknown workflow rpc op: {op}")
            await self._respond(rpc_id, ok=True, result=result)
        except Exception as exc:
            await self._respond(rpc_id, ok=False, error=str(exc))

    async def _respond(self, rpc_id: Any, *, ok: bool, result: Any = None, error: str | None = None) -> None:
        process = self._node_process
        if process is None or process.stdin is None or process.stdin.is_closing():
            return
        payload = {"id": rpc_id, "ok": ok, "result": result, "error": error}
        data = json.dumps(payload, ensure_ascii=True, default=str) + "\n"
        async with self._write_lock:
            process.stdin.write(data.encode("utf-8"))
            await process.stdin.drain()

    async def _agent(self, message: dict[str, Any]) -> Any:
        prompt = str(message.get("prompt") or "")
        opts = message.get("opts")
        if not isinstance(opts, dict):
            opts = {}
        phase = str(opts.get("phase") or self.run.current_phase or "") or None
        cache_key = workflow_cache_key(script_hash=self.run.script_hash, prompt=prompt, opts=opts)
        cached = self.store.read_cache(self.run.id, cache_key)
        agent_id = f"agent_{cache_key[:12]}"

        if cached is not None:
            self.run.cached_count += 1
            self._increment_phase(phase, cached=True)
            record = WorkflowAgentRecord(
                id=agent_id,
                cache_key=cache_key,
                prompt=prompt,
                phase=phase,
                status="cached",
                output=str(cached),
                model=str(opts.get("model") or self.model or ""),
                started_at=time.time(),
                ended_at=time.time(),
                cached=True,
            )
            self.run.agents[agent_id] = record
            self.store.write_snapshot(self.run)
            self.store.append_event(self.run.id, "agent_cached", {"agent_id": agent_id, "phase": phase, "cache_key": cache_key})
            return cached

        if self.run.agent_count >= self.max_agents:
            raise RuntimeError(f"workflow exceeded max agent calls ({self.max_agents})")

        self.run.agent_count += 1
        self._increment_phase(phase, cached=False)
        record = WorkflowAgentRecord(
            id=agent_id,
            cache_key=cache_key,
            prompt=prompt,
            phase=phase,
            status="pending",
            model=str(opts.get("model") or self.model or ""),
            metadata=dict(opts),
        )
        self.run.agents[agent_id] = record
        self.store.write_snapshot(self.run)

        async with self._semaphore:
            record.status = "running"
            record.started_at = time.time()
            self.store.write_snapshot(self.run)
            self.store.append_event(self.run.id, "agent_started", {"agent_id": agent_id, "phase": phase, "cache_key": cache_key})
            try:
                output, task_id, spawned_agent_id = await self.agent_runner.run_agent(
                    prompt=prompt,
                    cwd=Path(self.run.cwd),
                    model=str(opts.get("model") or self.model or "") or None,
                    phase=phase,
                    metadata=opts,
                )
            except Exception as exc:
                record.status = "failed"
                record.error = str(exc)
                record.ended_at = time.time()
                self.store.write_snapshot(self.run)
                self.store.append_event(self.run.id, "agent_failed", {"agent_id": agent_id, "error": str(exc)})
                raise

            record.status = "completed"
            record.task_id = task_id
            if spawned_agent_id:
                record.metadata["spawned_agent_id"] = spawned_agent_id
            record.output = output
            record.ended_at = time.time()
            self.store.write_cache(self.run.id, cache_key, output)
            self.store.write_snapshot(self.run)
            self.store.append_event(self.run.id, "agent_completed", {"agent_id": agent_id, "phase": phase, "task_id": task_id})
            return output

    def _workflow_start(self, name: str) -> dict[str, str]:
        self.run.name = name or self.run.name
        self.store.write_snapshot(self.run)
        self.store.append_event(self.run.id, "workflow_started", {"name": self.run.name})
        return {"name": self.run.name}

    def _workflow_end(self, name: str, result: Any) -> dict[str, str]:
        self.run.name = name or self.run.name
        self.run.result = result
        self.store.write_snapshot(self.run)
        self.store.append_event(self.run.id, "workflow_completed", {"name": self.run.name, "result": result})
        return {"name": self.run.name}

    def _workflow_error(self, name: str, error: str) -> dict[str, str]:
        self.run.name = name or self.run.name
        self.run.error = error
        self.store.write_snapshot(self.run)
        self.store.append_event(self.run.id, "workflow_error", {"name": self.run.name, "error": error})
        return {"name": self.run.name}

    def _phase_start(self, name: str) -> dict[str, str]:
        phase = self.run.phases.get(name)
        if phase is None:
            phase = WorkflowPhaseRecord(name=name)
            self.run.phases[name] = phase
        phase.status = "running"
        phase.started_at = phase.started_at or time.time()
        self.run.current_phase = name
        self.store.write_snapshot(self.run)
        self.store.append_event(self.run.id, "phase_started", {"phase": name})
        return {"phase": name}

    def _phase_end(self, name: str) -> dict[str, str]:
        phase = self.run.phases.get(name)
        if phase is None:
            phase = WorkflowPhaseRecord(name=name)
            self.run.phases[name] = phase
        phase.status = "completed"
        phase.ended_at = time.time()
        if self.run.current_phase == name:
            self.run.current_phase = None
        self.store.write_snapshot(self.run)
        self.store.append_event(self.run.id, "phase_completed", {"phase": name})
        return {"phase": name}

    def _log(self, message: str) -> dict[str, int]:
        if message:
            self.run.logs.append(message)
            self.run.logs = self.run.logs[-200:]
        self.store.write_snapshot(self.run)
        self.store.append_event(self.run.id, "log", {"message": message})
        return {"log_count": len(self.run.logs)}

    def _increment_phase(self, phase_name: str | None, *, cached: bool) -> None:
        if not phase_name:
            return
        phase = self.run.phases.get(phase_name)
        if phase is None:
            phase = WorkflowPhaseRecord(name=phase_name, status="running", started_at=time.time())
            self.run.phases[phase_name] = phase
        phase.agent_count += 1
        if cached:
            phase.cached_count += 1

    def _finalize_open_phases(self, status: str) -> None:
        now = time.time()
        for phase in self.run.phases.values():
            if phase.status == "running":
                phase.status = status
                phase.ended_at = phase.ended_at or now
        self.run.current_phase = None

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.readline()
            if not chunk:
                return
            text = chunk.decode("utf-8", errors="replace").strip()
            if text:
                self.store.append_event(self.run.id, "runtime_stderr", {"message": text})

    async def _kill_node(self) -> None:
        process = self._node_process
        if process is None or process.returncode is not None:
            return
        process.kill()
        await process.wait()


def default_max_concurrency() -> int:
    cpu_count = os.cpu_count() or 2
    return max(1, min(16, cpu_count - 2))


_NODE_DRIVER = textwrap.dedent(
    r"""
    const fs = require("fs");
    const vm = require("vm");
    const readline = require("readline");

    const scriptPath = process.argv[2];
    const source = fs.readFileSync(scriptPath, "utf8");
    const forbiddenPatterns = [
      /\bDate\s*\.\s*now\s*\(/,
      /\bnew\s+Date\s*\(/,
      /\bMath\s*\.\s*random\s*\(/,
      /\brequire\s*\(/,
      /\bimport\s*\(/,
      /\bfetch\s*\(/,
    ];
    for (const pattern of forbiddenPatterns) {
      if (pattern.test(source)) {
        throw new Error(`Forbidden workflow API matched ${pattern}`);
      }
    }

    let nextId = 1;
    const pending = new Map();
    const rl = readline.createInterface({ input: process.stdin });
    rl.on("line", line => {
      let msg;
      try {
        msg = JSON.parse(line);
      } catch (error) {
        return;
      }
      const waiter = pending.get(msg.id);
      if (!waiter) return;
      pending.delete(msg.id);
      if (msg.ok) {
        waiter.resolve(msg.result);
      } else {
        waiter.reject(new Error(msg.error || "workflow rpc failed"));
      }
    });

    const rootWorkflows = [];

    function rpc(op, payload) {
      const id = nextId++;
      const msg = Object.assign({ id, op }, payload || {});
      process.stdout.write(JSON.stringify(msg) + "\n");
      return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
    }

    function workflow(name, fn) {
      const run = (async () => {
        await rpc("workflow_start", { name });
        try {
          const result = await fn();
          await rpc("workflow_end", { name, result });
          return result;
        } catch (error) {
          await rpc("workflow_error", { name, error: String(error && error.stack || error) });
          throw error;
        }
      })();
      rootWorkflows.push(run);
      return run;
    }

    async function agent(prompt, opts) {
      return await rpc("agent", { prompt: String(prompt), opts: opts || {} });
    }

    async function parallel(items, fn) {
      if (!Array.isArray(items)) throw new Error("parallel(items, fn) requires an array");
      if (typeof fn !== "function") throw new Error("parallel(items, fn) requires a function");
      return await Promise.all(items.map((item, index) => fn(item, index)));
    }

    async function pipeline(items, stages) {
      if (!Array.isArray(items)) throw new Error("pipeline(items, stages) requires an array of items");
      if (!Array.isArray(stages)) throw new Error("pipeline(items, stages) requires an array of stage functions");
      return await Promise.all(items.map(async (item, index) => {
        let value = item;
        for (const stage of stages) {
          if (typeof stage !== "function") throw new Error("pipeline stages must be functions");
          value = await stage(value, index);
        }
        return value;
      }));
    }

    async function phase(name, fn) {
      await rpc("phase_start", { name: String(name) });
      try {
        return await fn();
      } finally {
        await rpc("phase_end", { name: String(name) });
      }
    }

    async function log(message) {
      await rpc("log", { message: String(message) });
    }

    const safeMath = Object.create(Math);
    safeMath.random = function () {
      throw new Error("Math.random is disabled in deterministic workflows");
    };

    function DisabledDate() {
      throw new Error("Date is disabled in deterministic workflows");
    }
    DisabledDate.now = function () {
      throw new Error("Date.now is disabled in deterministic workflows");
    };

    const context = {
      workflow,
      agent,
      parallel,
      pipeline,
      phase,
      log,
      console: { log: (...args) => log(args.join(" ")) },
      JSON,
      Array,
      Object,
      String,
      Number,
      Boolean,
      Promise,
      Error,
      RegExp,
      Map,
      Set,
      Math: safeMath,
      Date: DisabledDate,
    };

    async function main() {
      const wrapped = `(async () => {\n${source}\n})()`;
      const vmContext = vm.createContext(context, {
        codeGeneration: { strings: false, wasm: false },
      });
      const script = new vm.Script(wrapped, { filename: scriptPath });
      const result = await script.runInContext(vmContext, { timeout: 1000 });
      if (typeof result === "undefined" && rootWorkflows.length === 1) {
        return await rootWorkflows[0];
      }
      if (typeof result === "undefined" && rootWorkflows.length > 1) {
        return await Promise.all(rootWorkflows);
      }
      return result;
    }

    main()
      .then(result => {
        rl.close();
        process.stdout.write(JSON.stringify({ op: "__done", ok: true, result }) + "\n", () => process.exit(0));
      })
      .catch(error => {
        rl.close();
        process.stdout.write(JSON.stringify({ op: "__done", ok: false, error: String(error && error.stack || error) }) + "\n", () => process.exit(1));
      });
    """
).strip() + "\n"
