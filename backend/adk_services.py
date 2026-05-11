"""Shared ADK FastAPI/session factories — match env vars used by ADK CLI."""

from __future__ import annotations

import os
from pathlib import Path

from google.adk.cli.fast_api import get_fast_api_app

DEFAULT_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


def platform_root() -> Path:
    return Path(__file__).resolve().parent.parent


def agents_dir() -> str:
    return str(platform_root() / "adk_agents")


def default_adk_session_uri() -> str:
    db = platform_root() / "data" / "adk_sessions.db"
    return f"sqlite:///{db.as_posix()}"


def adk_session_service_uri() -> str:
    return os.getenv("ADK_SESSION_SERVICE_URI") or default_adk_session_uri()


def adk_memory_service_uri() -> str | None:
    return os.getenv("ADK_MEMORY_SERVICE_URI")


def adk_artifact_service_uri() -> str | None:
    return os.getenv("ADK_ARTIFACT_SERVICE_URI")


def adk_a2a_enabled() -> bool:
    return os.getenv("ADK_ENABLE_A2A", "").lower() in ("1", "true")


def build_adk_fastapi_app(*, allow_origins: list[str] | None = None):
    """Build the stock ADK FastAPI app mounted by this backend at `/adk`."""
    return get_fast_api_app(
        agents_dir=agents_dir(),
        session_service_uri=adk_session_service_uri(),
        memory_service_uri=adk_memory_service_uri(),
        artifact_service_uri=adk_artifact_service_uri(),
        web=False,
        a2a=adk_a2a_enabled(),
        url_prefix="/adk",
        allow_origins=allow_origins or DEFAULT_CORS_ORIGINS,
    )
