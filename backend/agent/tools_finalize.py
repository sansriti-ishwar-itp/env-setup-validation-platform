"""Persist structured validation outcome (human review gate)."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext

from ..services.run_store import SegmentRecord
from .tool_runtime import resolve_validation_runtime

logger = logging.getLogger(__name__)


def finalize_validation_plan(
    setup_instructions: str,
    segments_json: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """
    REQUIRED final step for phase 1. Saves proposed file edits as segments and
    marks the run as awaiting human approval. segments_json must be a JSON array of objects:
    [{"file_path": "...", "rationale": "...", "risk_level": "low|medium|high",
      "original_content": "...", "new_content": "..."}]
    """
    rt = resolve_validation_runtime(tool_context)
    if not rt.run_id:
        return {
            "status": "error",
            "error": "Could not resolve a run id from evs_run_id or the ADK session id.",
        }

    try:
        raw = json.loads(segments_json)
    except json.JSONDecodeError as exc:
        return {"status": "error", "error": f"Invalid segments JSON: {exc}"}

    if not isinstance(raw, list):
        return {"status": "error", "error": "segments_json must be a JSON array"}

    segments: list[SegmentRecord] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = item.get("file_path") or item.get("path")
        if not path:
            continue
        new_content = item.get("new_content")
        if new_content is None:
            continue
        seg_id = item.get("id") or f"seg-{uuid.uuid4().hex[:10]}"
        segments.append(
            SegmentRecord(
                id=str(seg_id),
                file_path=str(path),
                rationale=str(item.get("rationale", "")),
                risk_level=str(item.get("risk_level", "medium")),
                original_content=item.get("original_content"),
                new_content=str(new_content),
            )
        )

    if not segments:
        return {
            "status": "error",
            "error": "No valid segments; include file_path and new_content for each item.",
        }

    rt.store.save_analysis_result(
        rt.run_id,
        setup_instructions=setup_instructions,
        segments=segments,
    )
    rt.store.append_event(
        rt.run_id,
        "finalize",
        {"segment_count": len(segments)},
    )
    rt.gitlab.close()

    return {
        "status": "success",
        "segment_count": len(segments),
        "message": "Plan saved. Operator must approve segments before apply.",
    }


def finalize_validation_plan_tool() -> FunctionTool:
    return FunctionTool(finalize_validation_plan)
