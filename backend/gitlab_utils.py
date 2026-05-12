"""
GitLab configuration for env validation**:

- Project/branch: `modular_agents/agents/gitlab_utils.py`
- Token + API URL resolution: `modular_agents/agents/gitlab_automation_agent/server.py`
  (`_resolve_gitlab_auth`, default API URL when `GITLAB_API_URL` is unset).

Keep variable names identical so `.env` files can move between repos.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())


def get_gitlab_project_id() -> Optional[str]:
    """GITLAB_PROJECT_ID —  `get_gitlab_project_id()`."""
    project_id = os.getenv("GITLAB_PROJECT_ID")
    if project_id is None or not str(project_id).strip():
        return None
    return str(project_id).strip()


def get_configured_base_branch() -> Optional[str]:
    """GITLAB_BRANCH — `get_configured_base_branch()`."""
    branch = os.getenv("GITLAB_BRANCH")
    if branch is None or not str(branch).strip():
        return None
    return str(branch).strip()


def get_gitlab_branch() -> Optional[str]:
    """Alias for `get_configured_base_branch()` (target branch for reads/commits)."""
    return get_configured_base_branch()


def resolve_gitlab_auth() -> Tuple[Optional[str], str]:
    """
    Token + API URL — `_resolve_gitlab_auth()` in
    `gitlab_automation_agent/server.py`.
    """
    gitlab_token = (
        os.getenv("GITLAB_PERSONAL_ACCESS_TOKEN")
        or os.getenv("GITLAB_PERSONAL_TOKEN")
        or os.getenv("GITLAB_TOKEN")
        or os.getenv("GITLAB_ACCESS_TOKEN")
        or os.getenv("GLAB_TOKEN")
    )
    gitlab_api_url = (os.getenv("GITLAB_API_URL") or "").strip()
    if not gitlab_api_url:
        gitlab_api_url = "https://gitlab.com/api/v4"
    return gitlab_token, gitlab_api_url


def resolve_gitlab_token() -> Optional[str]:
    return resolve_gitlab_auth()[0]


def get_gitlab_api_url() -> str:
    return resolve_gitlab_auth()[1]
