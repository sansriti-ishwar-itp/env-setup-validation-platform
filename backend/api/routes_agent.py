"""Python-native agent runtime, tool execution, and state APIs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..agent.tool_runtime import (
    STATE_BRANCH,
    STATE_FILE_CONTENTS,
    STATE_PROJECT,
    STATE_RUN_ID,
)
from ..agent.tools_analysis import analyze_environment_setup
from ..agent.tools_discovery import discover_environment_files
from ..gitlab_utils import get_gitlab_branch, get_gitlab_project_id
from ..services.run_store import RunRecord, RunStore, SegmentRecord

router = APIRouter(prefix="/api/agent", tags=["agent"])

ToolName = Literal["discover_environment_files", "analyze_environment_setup"]


class ToolExecutionBody(BaseModel):
    tool_name: ToolName
    run_id: str | None = None
    project_id: str | None = None
    branch: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


@dataclass
class _SessionShim:
    id: str | None


class _ToolContextShim:
    """Small ToolContext-compatible state holder for direct Python tool calls."""

    def __init__(self, state: dict[str, Any], session_id: str | None) -> None:
        self.state = state
        self.session = _SessionShim(session_id)
        self.user_content = None


def _store(request: Request) -> RunStore:
    return request.app.state.store


def _segment_model(segment: SegmentRecord) -> dict[str, Any]:
    return {
        "id": segment.id,
        "file_path": segment.file_path,
        "rationale": segment.rationale,
        "risk_level": segment.risk_level,
        "original_content": segment.original_content,
        "new_content": segment.new_content,
        "approved": segment.approved,
        "apply_status": segment.apply_status,
        "apply_error": segment.apply_error,
    }


def _run_model(run: RunRecord) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "status": run.status,
        "goal": run.goal,
        "project_id": run.project_id,
        "branch": run.branch,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "error_message": run.error_message,
        "setup_instructions": run.setup_instructions,
        "segments": [_segment_model(segment) for segment in run.segments],
        "metadata": run.metadata,
    }


def _resolve_context(body: ToolExecutionBody, store: RunStore) -> _ToolContextShim:
    run = store.get_run(body.run_id) if body.run_id else None
    if body.run_id and run is None:
        raise HTTPException(404, detail="Run not found")

    project_id = body.project_id or run.project_id if run else body.project_id
    branch = body.branch or run.branch if run else body.branch
    project_id = project_id or get_gitlab_project_id()
    branch = branch or get_gitlab_branch()
    if not project_id or not branch:
        raise HTTPException(
            400,
            detail="project_id and branch required (request body, run_id, or environment)",
        )

    file_contents = body.arguments.get("file_contents")
    if file_contents is None:
        file_contents = {}
    if not isinstance(file_contents, dict):
        raise HTTPException(400, detail="arguments.file_contents must be an object when provided")

    return _ToolContextShim(
        state={
            STATE_PROJECT: str(project_id),
            STATE_BRANCH: str(branch),
            STATE_RUN_ID: body.run_id,
            STATE_FILE_CONTENTS: file_contents,
        },
        session_id=body.run_id,
    )


@router.get("/runtime")
async def runtime_metadata() -> dict[str, Any]:
    return {
        "runtime": "python",
        "description": "Python-native API layer for agent runtime, tool execution, and state.",
        "endpoints": {
            "runtime": "GET /api/agent/runtime",
            "tools": "GET /api/agent/tools",
            "execute_tool": "POST /api/agent/tools/execute",
            "state": "GET /api/agent/state/{run_id}",
            "managed_run": "POST /api/runs",
        },
        "state_keys": {
            STATE_PROJECT: "GitLab project id or path",
            STATE_BRANCH: "Target branch/ref",
            STATE_RUN_ID: "Run id linking tool events to persisted state",
            STATE_FILE_CONTENTS: "Discovered setup file contents used by analysis",
        },
        "adk": {
            "available": True,
            "mounted_at": "/adk",
            "note": "ADK remains available, but these /api/agent endpoints execute through Python directly.",
        },
    }


@router.get("/tools")
async def list_tools() -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": "discover_environment_files",
                "description": "Find and fetch environment setup files from GitLab.",
                "arguments": {"max_files": "optional integer, default 40"},
            },
            {
                "name": "analyze_environment_setup",
                "description": "Run deterministic setup checks against discovered file contents.",
                "arguments": {
                    "file_contents": "optional object of path -> text",
                    "file_contents_json": "optional JSON string of path -> text",
                    "auto_discover": "optional boolean, default true when file contents are absent",
                    "max_files": "optional integer for auto-discovery, default 40",
                },
            },
        ]
    }


@router.post("/tools/execute")
async def execute_tool(body: ToolExecutionBody, request: Request) -> dict[str, Any]:
    store = _store(request)
    context = _resolve_context(body, store)
    arguments = body.arguments
    max_files = int(arguments.get("max_files", 40))

    if body.tool_name == "discover_environment_files":
        result = discover_environment_files(max_files=max_files, tool_context=context)  # type: ignore[arg-type]
        return {
            "tool_name": body.tool_name,
            "state": {
                STATE_PROJECT: context.state[STATE_PROJECT],
                STATE_BRANCH: context.state[STATE_BRANCH],
                STATE_RUN_ID: context.state[STATE_RUN_ID],
            },
            "result": result,
        }

    if body.tool_name == "analyze_environment_setup":
        prelude = None
        file_contents_json = arguments.get("file_contents_json")
        if file_contents_json is not None and not isinstance(file_contents_json, str):
            file_contents_json = json.dumps(file_contents_json)

        has_contents = bool(context.state.get(STATE_FILE_CONTENTS))
        auto_discover = bool(arguments.get("auto_discover", not has_contents and not file_contents_json))
        if auto_discover and not has_contents and not file_contents_json:
            prelude = discover_environment_files(max_files=max_files, tool_context=context)  # type: ignore[arg-type]

        result = analyze_environment_setup(
            file_contents_json=file_contents_json or "",
            tool_context=context,  # type: ignore[arg-type]
        )
        return {
            "tool_name": body.tool_name,
            "state": {
                STATE_PROJECT: context.state[STATE_PROJECT],
                STATE_BRANCH: context.state[STATE_BRANCH],
                STATE_RUN_ID: context.state[STATE_RUN_ID],
            },
            "prelude": prelude,
            "result": result,
        }

    raise HTTPException(400, detail=f"Unsupported tool {body.tool_name}")


@router.get("/state/{run_id}")
async def get_agent_state(run_id: str, request: Request, after_event_id: int = 0) -> dict[str, Any]:
    store = _store(request)
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, detail="Run not found")
    return {
        "run": _run_model(run),
        "events": store.list_events_since(run_id, after_event_id),
    }
