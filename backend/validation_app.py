"""ADK App wrapper with resumability (human-in-the-loop / resume via invocationId)."""

from __future__ import annotations

from google.adk.apps.app import App, ResumabilityConfig

from .agent.root_agent import create_root_agent


def build_validation_app() -> App:
    """Single exported graph for CLI `adk web` and the mounted ADK API server."""
    return App(
        name="env_setup_validation",
        root_agent=create_root_agent(),
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
