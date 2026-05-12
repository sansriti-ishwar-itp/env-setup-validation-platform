"""Load configuration from environment (delegates GitLab to gitlab_utils)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from .gitlab_utils import (
    get_configured_base_branch,
    get_gitlab_api_url,
    get_gitlab_branch,
    get_gitlab_project_id,
    resolve_gitlab_auth,
    resolve_gitlab_token,
)

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _ROOT / "data" / "runs.db"


def _env(key: str, default: str | None = None) -> str | None:
    v = os.getenv(key)
    if v is None or v.strip() == "":
        return default
    return v.strip()


@lru_cache
def get_database_url() -> str:
    url = _env("DATABASE_URL")
    if url:
        return url
    return f"sqlite:///{_DEFAULT_DB.as_posix()}"


@lru_cache
def get_gemini_model() -> str:
    return _env("GEMINI_MODEL", "gemini-2.0-flash") or "gemini-2.0-flash"


__all__ = [
    "get_configured_base_branch",
    "get_database_url",
    "get_gemini_model",
    "get_gitlab_api_url",
    "get_gitlab_branch",
    "get_gitlab_project_id",
    "resolve_gitlab_auth",
    "resolve_gitlab_token",
]
