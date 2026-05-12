"""Discover ADK REST routes mounted under `/adk` (official ADK API surface)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/adk", tags=["adk"])


@router.get("/endpoints")
async def list_adk_endpoints() -> dict[str, object]:
    """
    This service mounts the stock ADK FastAPI app at `/adk`.
    Full OpenAPI for ADK is served at `/adk/openapi.json` (when enabled).
    """
    return {
        "mounted_at": "/adk",
        "spec": "/adk/openapi.json",
        "upstream_docs": "https://google.github.io/adk-docs/api-reference/rest/",
        "session_state_keys_for_this_app": {
            "evs_project_id": "GitLab project id or path (same as GITLAB_PROJECT_ID)",
            "evs_branch": "Branch ref (same as GITLAB_BRANCH)",
            "evs_run_id": "UUID linking to SQLite runs table (set automatically by POST /api/runs)",
            "evs_file_contents": "Populated by discover_environment_files tool",
        },
        "run_agent": {
            "streaming": "POST /adk/run_sse",
            "blocking": "POST /adk/run",
            "resume": "POST /adk/run_sse with invocationId (requires resumable App — see validation_app.py)",
        },
        "sessions": {
            "create": "POST /adk/apps/env_setup_validation/users/{user_id}/sessions/{session_id}",
            "get": "GET /adk/apps/env_setup_validation/users/{user_id}/sessions/{session_id}",
            "list": "GET /adk/apps/env_setup_validation/users/{user_id}/sessions",
            "delete": "DELETE /adk/apps/env_setup_validation/users/{user_id}/sessions/{session_id}",
            "patch": "PATCH /adk/apps/env_setup_validation/users/{user_id}/sessions/{session_id}",
        },
        "debug_traces": {
            "by_event": "GET /adk/debug/trace/{event_id}",
            "by_session": "GET /adk/debug/trace/session/{session_id}",
        },
        "misc": {
            "health": "GET /adk/health",
            "version": "GET /adk/version",
            "list_apps": "GET /adk/list-apps",
            "artifacts_prefix": "/adk/apps/{app_name}/users/{user_id}/sessions/{session_id}/artifacts",
            "memory_patch": "PATCH /adk/apps/{app_name}/users/{user_id}/memory",
        },
        "alignment": {
            "reference": "modular_agents/api/routers/agents.py — execute_agent_with_adk",
            "session_seed": (
                "POST /adk/apps/{app_name}/users/{user_id}/sessions/{session_id} "
                "with JSON body containing initial session state"
            ),
            "run_sse_body_shape": {
                "appName": "env_setup_validation",
                "userId": "operator",
                "sessionId": "<uuid>",
                "newMessage": {"role": "user", "parts": [{"text": "..."}]},
                "streaming": False,
                "invocationId": "(optional, resume — same as ADK docs)",
            },
            "gitlab_env": "modular_agents/agents/gitlab_utils.py + gitlab_automation_agent/server.py _resolve_gitlab_auth",
        },
    }
