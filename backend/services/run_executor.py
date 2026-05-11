"""
Execute ADK analysis phase through the stock ADK `/run_sse` endpoint.

Neurostack parity: matches the intent of `execute_agent_with_adk` in
`modular_agents/api/routers/agents.py` — seed session state, then invoke the agent
over the official ADK HTTP protocol with the same session keys and GitLab env contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from ..agent.tool_runtime import (
    STATE_BRANCH,
    STATE_FILE_CONTENTS,
    STATE_PROJECT,
    STATE_RUN_ID,
)
from .run_store import RunStore

logger = logging.getLogger(__name__)

ADK_APP_NAME = "env_setup_validation"
ADK_USER_ID = "operator"


def _serialize_adk_event(event: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_type": "adk_event",
        "id": event.get("id"),
        "author": event.get("author"),
        "invocation_id": event.get("invocationId"),
    }

    content = event.get("content")
    if isinstance(content, dict):
        parts = content.get("parts")
        if isinstance(parts, list):
            texts: list[str] = []
            calls: list[dict[str, Any]] = []
            responses: list[dict[str, Any]] = []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    texts.append(text[:8000])
                function_call = part.get("functionCall")
                if isinstance(function_call, dict):
                    calls.append(
                        {
                            "name": function_call.get("name"),
                            "id": function_call.get("id"),
                        }
                    )
                function_response = part.get("functionResponse")
                if isinstance(function_response, dict):
                    responses.append(
                        {
                            "name": function_response.get("name"),
                            "id": function_response.get("id"),
                        }
                    )
            if texts:
                payload["text"] = texts
            if calls:
                payload["function_calls"] = calls
            if responses:
                payload["function_responses"] = responses

    return payload


async def _raise_for_adk_error(response: httpx.Response, action: str) -> None:
    if response.status_code < 400:
        return
    body = (await response.aread()).decode(errors="replace")[:2000]
    raise RuntimeError(f"ADK {action} failed ({response.status_code}): {body}")


async def _stream_adk_run(
    *,
    adk_app: Any,
    run_id: str,
    goal: str,
    store: RunStore,
    project_id: str,
    branch: str,
) -> None:
    transport = httpx.ASGITransport(app=adk_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://adk.local",
        timeout=httpx.Timeout(None),
    ) as client:
        session_response = await client.post(
            f"/apps/{ADK_APP_NAME}/users/{ADK_USER_ID}/sessions/{run_id}",
            json={
                STATE_PROJECT: project_id,
                STATE_BRANCH: branch,
                STATE_RUN_ID: run_id,
                STATE_FILE_CONTENTS: {},
            },
        )
        await _raise_for_adk_error(session_response, "session create")

        run_body = {
            "appName": ADK_APP_NAME,
            "userId": ADK_USER_ID,
            "sessionId": run_id,
            "newMessage": {"role": "user", "parts": [{"text": goal}]},
            "streaming": False,
        }

        data_lines: list[str] = []
        async with client.stream("POST", "/run_sse", json=run_body) as response:
            await _raise_for_adk_error(response, "run_sse")
            async for line in response.aiter_lines():
                if not line:
                    if data_lines:
                        _append_sse_payload(store, run_id, "\n".join(data_lines))
                        data_lines.clear()
                    continue
                if line.startswith("data:"):
                    data_lines.append(line.removeprefix("data:").strip())

        if data_lines:
            _append_sse_payload(store, run_id, "\n".join(data_lines))


def _append_sse_payload(store: RunStore, run_id: str, raw_payload: str) -> None:
    try:
        event = json.loads(raw_payload)
    except json.JSONDecodeError:
        store.append_event(run_id, "adk_event", {"raw": raw_payload[:8000]})
        return
    if isinstance(event, dict):
        store.append_event(run_id, "adk_event", _serialize_adk_event(event))
    else:
        store.append_event(run_id, "adk_event", {"raw": event})


async def run_analysis_phase(
    run_id: str,
    goal: str,
    store: RunStore,
    project_id: str,
    branch: str,
    adk_app: Any,
) -> None:
    from ..config import resolve_gitlab_token

    if not resolve_gitlab_token():
        store.update_run_status(
            run_id,
            "failed",
            "Missing GitLab token (set GITLAB_PERSONAL_ACCESS_TOKEN or GITLAB_TOKEN).",
        )
        return

    try:
        store.update_run_status(run_id, "running")
        store.append_event(
            run_id,
            "run_started",
            {
                "goal": goal,
                "adk_endpoint": "/adk/run_sse",
                "adk_session": (
                    f"/adk/apps/{ADK_APP_NAME}/users/{ADK_USER_ID}/sessions/{run_id}"
                ),
            },
        )

        await _stream_adk_run(
            adk_app=adk_app,
            run_id=run_id,
            goal=goal,
            store=store,
            project_id=project_id,
            branch=branch,
        )

        run = store.get_run(run_id)
        if run is None:
            return
        if run.status == "awaiting_approval":
            store.append_event(
                run_id,
                "phase1_complete",
                {"message": "Awaiting human approval"},
            )
            return
        if run.status == "running":
            store.update_run_status(
                run_id,
                "failed",
                "Agent completed without finalize_validation_plan; inspect /adk/debug/trace "
                f"or GET /api/runs/{run_id}/events.",
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("run_analysis_phase failed")
        store.update_run_status(run_id, "failed", str(exc))
        store.append_event(run_id, "error", {"message": str(exc)})


def spawn_analysis_task(
    *,
    run_id: str,
    goal: str,
    store: RunStore,
    project_id: str | None,
    branch: str | None,
    adk_app: Any,
) -> asyncio.Task[None]:
    from ..config import get_gitlab_branch, get_gitlab_project_id

    pid = project_id or get_gitlab_project_id()
    br = branch or get_gitlab_branch()
    if not pid or not br:
        raise ValueError(
            "project_id and branch are required (body or GITLAB_PROJECT_ID / GITLAB_BRANCH)"
        )

    return asyncio.create_task(
        run_analysis_phase(run_id, goal, store, pid, br, adk_app),
        name=f"analysis-{run_id}",
    )


def validate_apply_prerequisites(store: RunStore, run_id: str) -> tuple[bool, str]:
    run = store.get_run(run_id)
    if not run:
        return False, "Run not found"
    if run.status != "awaiting_approval":
        return False, f"Run must be awaiting_approval, got {run.status}"
    if not any(s.approved for s in run.segments):
        return False, "No approved segments to apply"
    return True, ""
