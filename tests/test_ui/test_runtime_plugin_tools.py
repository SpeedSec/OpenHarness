from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from openharness.tools.base import ToolExecutionContext
from openharness.ui.runtime import build_runtime, close_runtime


class _StaticApiClient:
    async def stream_message(self, request):
        del request
        if False:
            yield None


class _FacadeClient:
    @staticmethod
    def _base_payload():
        return {
            "candidate_only": True,
            "real_target_touch": False,
            "real_src_target_touch": False,
            "platform_submissions": 0,
            "artifact_refs": ["artifact:phase7/client_facade_smoke/fixture-run-1"],
            "provenance_refs": ["provenance:phase6/openharness_worker_run"],
        }

    def healthcheck(self):
        return {
            **self._base_payload(),
            "candidate_only": True,
            "real_target_touch": False,
            "real_src_target_touch": False,
            "platform_submissions": 0,
            "status": "ok",
        }

    def run_fixture_task(self, **payload):
        return {
            **self._base_payload(),
            "kind": "fixture",
            "payload": payload,
            "status": "completed",
        }

    def get_artifact_refs(self, payload):
        return {
            **self._base_payload(),
            "kind": "artifact_refs",
            "payload": payload,
            "status": "ok",
        }


class _UnsafeFacadeClient(_FacadeClient):
    def run_fixture_task(self, **payload):
        return {
            **self._base_payload(),
            "artifact_refs": [],
            "provenance_refs": ["provenance:phase6/openharness_worker_run"],
            "status": "completed",
            "summary": "confirmed vulnerability",
            "payload": payload,
        }


def _patch_srchunter_facade(monkeypatch) -> None:
    monkeypatch.setattr(
        "srchunter.adapters.openharness.client.SRCHunterOpenHarnessClient",
        _FacadeClient,
    )
    module = sys.modules.get(
        "openharness.plugins.bundled.srchunter.tools.srchunter_openharness_tool"
    )
    if module is not None:
        monkeypatch.setattr(module, "SRCHunterOpenHarnessClient", _FacadeClient)


def _patch_unsafe_srchunter_facade(monkeypatch) -> None:
    monkeypatch.setattr(
        "srchunter.adapters.openharness.client.SRCHunterOpenHarnessClient",
        _UnsafeFacadeClient,
    )
    module = sys.modules.get(
        "openharness.plugins.bundled.srchunter.tools.srchunter_openharness_tool"
    )
    if module is not None:
        monkeypatch.setattr(module, "SRCHunterOpenHarnessClient", _UnsafeFacadeClient)


def _patch_tool_smoke_path(monkeypatch, tmp_path: Path):
    from openharness.plugins.bundled.srchunter.tools import (
        srchunter_openharness_tool as tool_module,
    )

    phase7_root = tmp_path / "phase7"
    monkeypatch.setattr(tool_module, "PHASE7_ROOT", phase7_root)
    monkeypatch.setattr(tool_module, "_TOOL_SMOKE_PATH", phase7_root / "openharness_tool_smoke.json")
    return tool_module


def _patch_real_srchunter_facade(monkeypatch) -> None:
    from srchunter.adapters.openharness.client import SRCHunterOpenHarnessClient

    module = sys.modules.get(
        "openharness.plugins.bundled.srchunter.tools.srchunter_openharness_tool"
    )
    if module is not None:
        monkeypatch.setattr(module, "SRCHunterOpenHarnessClient", SRCHunterOpenHarnessClient)


def _write_tool_plugin(plugins_root: Path) -> None:
    plugin_dir = plugins_root / "tool-plugin"
    tools_dir = plugin_dir / "tools"
    tools_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "tool-plugin",
                "version": "1.0.0",
                "description": "Runtime tool plugin",
                "enabled_by_default": True,
            }
        ),
        encoding="utf-8",
    )
    (tools_dir / "echo_tool.py").write_text(
        "from pydantic import BaseModel\n"
        "from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult\n\n"
        "class EchoArgs(BaseModel):\n"
        "    text: str = 'hello'\n\n"
        "class EchoTool(BaseTool):\n"
        "    name = 'plugin_echo'\n"
        "    description = 'Echo from plugin tool'\n"
        "    input_model = EchoArgs\n\n"
        "    async def execute(self, arguments: EchoArgs, context: ToolExecutionContext) -> ToolResult:\n"
        "        del context\n"
        "        return ToolResult(output=arguments.text)\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_build_runtime_registers_enabled_plugin_tools(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    project = tmp_path / "repo"
    plugins_root = project / ".openharness" / "plugins"
    plugins_root.mkdir(parents=True)
    _write_tool_plugin(plugins_root)

    from openharness.config.settings import Settings

    monkeypatch.setattr("openharness.ui.runtime.load_settings", lambda: Settings(allow_project_plugins=True))

    bundle = await build_runtime(
        cwd=str(project),
        api_client=_StaticApiClient(),
    )
    try:
        tool = bundle.tool_registry.get("plugin_echo")
        assert tool is not None
        assert tool.description == "Echo from plugin tool"
    finally:
        await close_runtime(bundle)


@pytest.mark.asyncio
async def test_build_runtime_registers_bundled_srchunter_tool(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    from openharness.config.settings import Settings

    _patch_srchunter_facade(monkeypatch)
    monkeypatch.setattr("openharness.ui.runtime.load_settings", lambda: Settings())

    bundle = await build_runtime(
        cwd=str(tmp_path),
        api_client=_StaticApiClient(),
    )
    try:
        tool = bundle.tool_registry.get("srchunter")
        assert tool is not None
        result = await tool.execute(
            tool.input_model.model_validate({"action": "srchunter_healthcheck"}),
            ToolExecutionContext(cwd=tmp_path),
        )
        assert "\"status\": \"ok\"" in result.output
    finally:
        await close_runtime(bundle)


@pytest.mark.asyncio
async def test_bundled_srchunter_tool_is_thin_wrapper(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    _patch_srchunter_facade(monkeypatch)

    tool_module = _patch_tool_smoke_path(monkeypatch, tmp_path)

    tool = tool_module.SRCHunterTool()
    result = await tool.execute(
        tool.input_model.model_validate(
            {
                "action": "srchunter_run_fixture",
                "payload": {
                    "session_ref": "s1",
                    "task_ref": "t1",
                    "fixture_ref": "fixture/demo",
                    "operator_input_ref": "op/ref",
                },
            }
        ),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is False
    output = json.loads(result.output)
    assert output["kind"] == "fixture"
    assert output["tool"] == "srchunter"
    assert output["operation"] == "srchunter_run_fixture"
    assert output["tool_output_is_verdict"] is False
    assert output["artifact_refs"]
    assert output["candidate_only"] is True
    assert output["real_target_touch"] is False
    assert output["real_src_target_touch"] is False
    assert output["platform_submissions"] == 0
    smoke = tmp_path / "phase7" / "openharness_tool_smoke.json"
    assert smoke.exists()


@pytest.mark.asyncio
async def test_bundled_srchunter_tool_rejects_verdict_or_empty_refs(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    _patch_unsafe_srchunter_facade(monkeypatch)

    from openharness.plugins.bundled.srchunter.tools.srchunter_openharness_tool import SRCHunterTool

    tool = SRCHunterTool()
    result = await tool.execute(
        tool.input_model.model_validate(
            {
                "action": "srchunter_run_fixture",
                "payload": {
                    "session_ref": "s1",
                    "task_ref": "t1",
                    "fixture_ref": "fixture/demo",
                    "operator_input_ref": "op/ref",
                },
            }
        ),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is True
    assert "verdict" in result.output or "artifact_refs" in result.output


@pytest.mark.asyncio
async def test_bundled_srchunter_tool_does_not_accept_real_target_url(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    _patch_real_srchunter_facade(monkeypatch)

    from openharness.plugins.bundled.srchunter.tools.srchunter_openharness_tool import SRCHunterTool

    tool = SRCHunterTool()
    result = await tool.execute(
        tool.input_model.model_validate(
            {
                "action": "srchunter_run_fixture",
                "payload": {
                    "session_ref": "s1",
                    "task_ref": "t1",
                    "fixture_ref": "phase1/idor_positive.json",
                    "operator_input_ref": "op/ref",
                    "target_ref": "https://real-src.example",
                },
            }
        ),
        ToolExecutionContext(cwd=tmp_path),
    )

    assert result.is_error is True
    assert "real target" in result.output
