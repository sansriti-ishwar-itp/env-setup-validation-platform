"""Resolve GitLab + run persistence from ADK ToolContext.session.state."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from google.adk.tools.tool_context import ToolContext

from ..gitlab_utils import resolve_gitlab_auth
from ..services.gitlab_client import GitLabClient
from ..services.run_store import RunStore
from ..services.store_singleton import get_run_store

logger = logging.getLogger(__name__)

STATE_PROJECT = "evs_project_id"
STATE_BRANCH = "evs_branch"
STATE_RUN_ID = "evs_run_id"
STATE_FILE_CONTENTS = "evs_file_contents"


@dataclass
class ValidationToolRuntime:
    store: RunStore
    gitlab: GitLabClient
    project_id: str
    branch: str
    run_id: str | None
    state: Any


def _session_id(tool_context: ToolContext) -> str | None:
    session = getattr(tool_context, "session", None)
    sid = getattr(session, "id", None)
    return str(sid) if sid else None


def _user_goal(tool_context: ToolContext) -> str:
    content = getattr(tool_context, "user_content", None)
    parts = getattr(content, "parts", None)
    if parts:
        texts = []
        for part in parts:
            text = getattr(part, "text", None)
            if text:
                texts.append(str(text))
        if texts:
            return "\n".join(texts)
    return "ADK Web environment validation"


def resolve_validation_runtime(tool_context: ToolContext | None) -> ValidationToolRuntime:
    """
    Session state is seeded by POST /api/runs; ADK Web falls back to env vars.

    Keys: evs_project_id, evs_branch, evs_run_id (optional), evs_file_contents (optional dict).
    """
    if tool_context is None:
        raise RuntimeError(
            "ToolContext missing — tools must run through ADK run_sse with session state."
        )

    from ..config import get_gitlab_branch, get_gitlab_project_id

    store = get_run_store()
    st = tool_context.state
    project_id = str(st.get(STATE_PROJECT) or get_gitlab_project_id() or "")
    branch = str(st.get(STATE_BRANCH) or get_gitlab_branch() or "")
    run_id_raw = st.get(STATE_RUN_ID)
    run_id = str(run_id_raw) if run_id_raw else _session_id(tool_context)

    if not project_id or not branch:
        raise RuntimeError(
            "Session state must include evs_project_id and evs_branch, or set "
            "GITLAB_PROJECT_ID and GITLAB_BRANCH in the environment."
        )

    st[STATE_PROJECT] = project_id
    st[STATE_BRANCH] = branch
    if run_id:
        st[STATE_RUN_ID] = run_id
        store.ensure_run(
            run_id,
            goal=_user_goal(tool_context),
            project_id=project_id,
            branch=branch,
            meta={"source": "adk_web_or_direct_adk"},
        )

    if STATE_FILE_CONTENTS not in st or st.get(STATE_FILE_CONTENTS) is None:
        st[STATE_FILE_CONTENTS] = {}

    token, api_url = resolve_gitlab_auth()
    if not token:
        raise RuntimeError("GitLab token not configured (GITLAB_PERSONAL_ACCESS_TOKEN / GITLAB_TOKEN)")

    gl = GitLabClient(token=token, api_url=api_url)
    return ValidationToolRuntime(
        store=store,
        gitlab=gl,
        project_id=project_id,
        branch=branch,
        run_id=run_id,
        state=st,
    )


def file_contents_dict(rt: ValidationToolRuntime) -> dict[str, str]:
    raw = rt.state.get(STATE_FILE_CONTENTS)
    return raw if isinstance(raw, dict) else {}
