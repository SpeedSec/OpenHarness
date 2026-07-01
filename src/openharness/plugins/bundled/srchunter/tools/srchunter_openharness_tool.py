"""Bundled SRCHunter facade tool."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from srchunter.adapters.openharness.client import (
    PHASE7_ROOT,
    SRCHunterOpenHarnessClient,
)

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult

SRCHunterToolAction = Literal[
    "srchunter_healthcheck",
    "srchunter_run_fixture",
    "srchunter_run_scoped_task",
    "srchunter_get_artifacts",
]

_DEFAULT_ACTION: SRCHunterToolAction = "srchunter_healthcheck"
_TOOL_SMOKE_PATH = PHASE7_ROOT / "openharness_tool_smoke.json"


class SRCHunterToolInput(BaseModel):
    """Arguments for the bundled SRCHunter facade tool."""

    action: SRCHunterToolAction = Field(
        default=_DEFAULT_ACTION,
        description="Facade action to invoke.",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON-safe payload for the selected action.",
    )


class SRCHunterTool(BaseTool):
    """Thin wrapper around the SRCHunter OpenHarness client facade."""

    name = "srchunter"
    description = "Call the bundled SRCHunter OpenHarness facade."
    input_model = SRCHunterToolInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        del context
        tool_input = SRCHunterToolInput.model_validate(arguments)
        client = SRCHunterOpenHarnessClient()
        try:
            if tool_input.action == "srchunter_healthcheck":
                facade_payload = client.healthcheck()
            elif tool_input.action == "srchunter_run_fixture":
                facade_payload = client.run_fixture_task(**tool_input.payload)
            elif tool_input.action == "srchunter_run_scoped_task":
                facade_payload = client.run_scoped_task(tool_input.payload)
            elif tool_input.action == "srchunter_get_artifacts":
                facade_payload = client.get_artifact_refs(tool_input.payload)
            else:  # pragma: no cover - Literal keeps this unreachable in practice
                raise ValueError(f"Unsupported action: {tool_input.action}")
        except Exception as exc:
            return ToolResult(output=str(exc), is_error=True)

        try:
            payload = _tool_payload(tool_input.action, facade_payload)
            if (
                tool_input.action == "srchunter_run_fixture"
                and payload.get("facade_status") == "completed"
            ):
                _write_tool_smoke_artifact(payload)
        except Exception as exc:
            return ToolResult(output=str(exc), is_error=True)

        return ToolResult(
            output=json.dumps(payload, ensure_ascii=True, sort_keys=True),
            metadata={"payload": payload},
        )


def _tool_payload(operation: str, facade_payload: dict[str, Any]) -> dict[str, Any]:
    facade_status = facade_payload.get("status", "ok")
    scoped_output = operation == "srchunter_run_scoped_task" or (
        facade_payload.get("goal") == "9.B2"
    )
    payload = {
        **facade_payload,
        "goal": "9.B3" if scoped_output else "7.1",
        "tool": "srchunter",
        "operation": operation,
        "status": "ok" if facade_status == "completed" else facade_status,
        "facade_status": facade_status,
        "client_facade_used": True,
        "tool_output_is_verdict": False,
        "tool_output_self_proves_vulnerability": False,
    }
    if operation == "srchunter_run_fixture":
        artifact_refs = _string_list(facade_payload, "artifact_refs")
        tool_ref = "artifact:phase7/openharness_tool_smoke/fixture-run-1"
        payload["artifact_refs"] = [
            tool_ref,
            *(ref for ref in artifact_refs if ref != tool_ref),
        ]
    _assert_tool_boundary(payload)
    _assert_not_verdict(payload)
    return payload


def _write_tool_smoke_artifact(payload: dict[str, Any]) -> None:
    artifact_refs = _string_list(payload, "artifact_refs")
    if not artifact_refs:
        raise ValueError("artifact_refs cannot be empty for OpenHarness tool smoke")
    provenance_refs = _string_list(payload, "provenance_refs")
    PHASE7_ROOT.mkdir(parents=True, exist_ok=True)
    smoke = {
        "goal": "7.1",
        "openharness_tool_registered": True,
        "client_facade_used": True,
        "tool": "srchunter",
        "operation": payload["operation"],
        "status": payload["status"],
        "artifact_refs": artifact_refs,
        "provenance_refs": [
            "client:SRCHunterOpenHarnessClient",
            *provenance_refs,
        ],
        "tool_output_is_verdict": False,
        "candidate_only": True,
        "real_target_touch": False,
        "real_src_target_touch": False,
        "platform_submissions": 0,
    }
    _TOOL_SMOKE_PATH.write_text(
        json.dumps(smoke, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _assert_tool_boundary(payload: dict[str, Any]) -> None:
    if payload.get("candidate_only") is not True:
        raise ValueError("candidate_only must be true")
    if payload.get("operation") == "srchunter_run_scoped_task" or payload.get(
        "goal"
    ) == "9.B3":
        if not isinstance(payload.get("real_target_touch"), bool):
            raise ValueError("real_target_touch must be boolean")
    elif payload.get("real_target_touch") is not False:
        raise ValueError("real_target_touch must be false")
    if payload.get("real_src_target_touch") is not False:
        raise ValueError("real_src_target_touch must be false")
    if payload.get("platform_submissions") != 0:
        raise ValueError("platform_submissions must be 0")


def _assert_not_verdict(payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=True, sort_keys=True).lower()
    forbidden = (
        "confirmed vulnerability",
        "verified exploit",
        "platform submission",
    )
    if any(term in rendered for term in forbidden):
        raise ValueError("SRCHunter tool output cannot contain vulnerability verdicts")


def _string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{key} must be a string list")
    return list(value)
