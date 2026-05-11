"""Request / response models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CreateRunBody(BaseModel):
    goal: str = Field(default="Validate my Python project setup")
    project_id: str | None = None
    branch: str | None = None


class SegmentDecision(BaseModel):
    id: str
    approved: bool = False
    edited_content: str | None = None


class ApprovalBody(BaseModel):
    segments: list[SegmentDecision]


class RunSummary(BaseModel):
    run_id: str
    status: str
    goal: str
    project_id: str
    branch: str
    created_at: str
    updated_at: str
    error_message: str | None = None
    setup_instructions: str | None = None
    segments: list[dict[str, Any]]
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateRunResponse(BaseModel):
    run_id: str
    status: Literal["accepted"] = "accepted"
