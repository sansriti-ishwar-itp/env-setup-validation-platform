"""Root ADK agent with discovery, analysis, and finalize tools (read-only until approval)."""

from __future__ import annotations

from google.adk import Agent
from google.genai import types

from ..config import get_gemini_model
from .prompts import SYSTEM_PROMPT
from .tools_analysis import analyze_environment_setup_tool
from .tools_discovery import discover_environment_files_tool
from .tools_finalize import finalize_validation_plan_tool


def create_root_agent() -> Agent:
    model = get_gemini_model()
    return Agent(
        name="env_setup_validation_agent",
        model=model,
        description="Validates Python/container environment setup from GitLab; proposes patches for human approval.",
        instruction=SYSTEM_PROMPT,
        tools=[
            discover_environment_files_tool(),
            analyze_environment_setup_tool(),
            finalize_validation_plan_tool(),
        ],
        generate_content_config=types.GenerateContentConfig(
            safety_settings=[
                types.SafetySetting(
                    category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                    threshold=types.HarmBlockThreshold.OFF,
                )
            ]
        ),
    )
