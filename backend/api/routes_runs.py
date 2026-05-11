"""Run lifecycle: create, poll, SSE, approval, apply."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..gitlab_utils import get_gitlab_branch, get_gitlab_project_id, resolve_gitlab_auth
from ..services.gitlab_client import GitLabClient
from ..services.run_executor import spawn_analysis_task, validate_apply_prerequisites
from ..services.run_store import RunStore, SegmentRecord
from .models import ApprovalBody, CreateRunBody, CreateRunResponse, RunSummary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runs", tags=["runs"])


def _store(request: Request) -> RunStore:
    return request.app.state.store


@router.post("", response_model=CreateRunResponse)
async def create_run(body: CreateRunBody, request: Request) -> CreateRunResponse:
    store = _store(request)
    pid = body.project_id or get_gitlab_project_id()
    branch = body.branch or get_gitlab_branch()
    if not pid or not branch:
        raise HTTPException(
            400,
            detail="project_id and branch required (request body or GITLAB_PROJECT_ID / GITLAB_BRANCH)",
        )
    run_id = store.create_run(
        goal=body.goal,
        project_id=pid,
        branch=branch,
        meta={"source": "api"},
    )
    spawn_analysis_task(
        run_id=run_id,
        goal=body.goal,
        store=store,
        project_id=pid,
        branch=branch,
        adk_app=request.app.state.adk_app,
    )
    return CreateRunResponse(run_id=run_id)


@router.get("/{run_id}", response_model=RunSummary)
async def get_run(run_id: str, request: Request) -> RunSummary:
    store = _store(request)
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, detail="Run not found")
    return RunSummary(
        run_id=run.run_id,
        status=run.status,
        goal=run.goal,
        project_id=run.project_id,
        branch=run.branch,
        created_at=run.created_at,
        updated_at=run.updated_at,
        error_message=run.error_message,
        setup_instructions=run.setup_instructions,
        segments=[_segment_model(s) for s in run.segments],
        metadata=run.metadata,
    )


def _segment_model(s: SegmentRecord) -> dict[str, Any]:
    return {
        "id": s.id,
        "file_path": s.file_path,
        "rationale": s.rationale,
        "risk_level": s.risk_level,
        "original_content": s.original_content,
        "new_content": s.new_content,
        "approved": s.approved,
        "apply_status": s.apply_status,
        "apply_error": s.apply_error,
    }


@router.get("/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    last_event_id: int = 0,
) -> StreamingResponse:
    store = _store(request)

    async def gen():
        last = last_event_id
        while True:
            run = store.get_run(run_id)
            if not run:
                break
            batch = store.list_events_since(run_id, last)
            for ev in batch:
                last = ev["id"]
                payload = json.dumps(
                    {
                        "id": ev["id"],
                        "ts": ev["ts"],
                        "kind": ev["kind"],
                        "payload": ev["payload"],
                    }
                )
                yield f"id: {ev['id']}\ndata: {payload}\n\n"

            if run.status in ("failed", "cancelled", "completed"):
                break
            if run.status == "awaiting_approval":
                await asyncio.sleep(0.6)
                for ev in store.list_events_since(run_id, last):
                    last = ev["id"]
                    payload = json.dumps(
                        {
                            "id": ev["id"],
                            "ts": ev["ts"],
                            "kind": ev["kind"],
                            "payload": ev["payload"],
                        }
                    )
                    yield f"id: {ev['id']}\ndata: {payload}\n\n"
                break
            if run.status not in ("pending", "running", "applying"):
                break
            await asyncio.sleep(0.35)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/{run_id}/approval")
async def approve_segments(run_id: str, body: ApprovalBody, request: Request) -> dict[str, Any]:
    store = _store(request)
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, detail="Run not found")
    if run.status != "awaiting_approval":
        raise HTTPException(400, detail=f"Run not awaiting approval (status={run.status})")

    by_id = {s.id: s for s in run.segments}
    for dec in body.segments:
        seg = by_id.get(dec.id)
        if seg is None:
            continue
        seg.approved = dec.approved
        if dec.edited_content is not None:
            seg.new_content = dec.edited_content

    store.update_segments(run_id, run.segments)
    store.append_event(run_id, "approval", {"updated": [d.id for d in body.segments]})
    return {"status": "ok", "segments": [_segment_model(s) for s in run.segments]}


@router.post("/{run_id}/apply")
async def apply_segments(run_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    ok, msg = validate_apply_prerequisites(store, run_id)
    if not ok:
        raise HTTPException(400, detail=msg)

    token, api_url = resolve_gitlab_auth()
    if not token:
        raise HTTPException(500, detail="GitLab token not configured")

    run = store.get_run(run_id)
    assert run is not None

    store.update_run_status(run_id, "applying")
    store.append_event(run_id, "apply_started", {})

    updated_segments: list[SegmentRecord] = []
    for seg in run.segments:
        if not seg.approved:
            seg.apply_status = "skipped"
        else:
            seg.apply_status = "pending"
        updated_segments.append(seg)

    commit_files = [
        {"path": s.file_path, "content": s.new_content}
        for s in run.segments
        if s.approved
    ]

    gitlab = GitLabClient(token=token, api_url=api_url)
    try:
        result = gitlab.push_files(
            run.project_id,
            run.branch,
            commit_files,
            commit_message=f"chore(env): validation fixes from run {run_id[:8]}",
        )
        if result.get("error"):
            store.update_run_status(run_id, "failed", result["error"])
            for seg in updated_segments:
                if seg.approved:
                    seg.apply_status = "failed"
                    seg.apply_error = result["error"]
            store.update_segments(run_id, updated_segments)
            store.append_event(run_id, "apply_failed", {"error": result["error"]})
            raise HTTPException(502, detail=result["error"])

        for seg in updated_segments:
            if seg.approved:
                seg.apply_status = "applied"
        store.update_segments(run_id, updated_segments)
        store.update_run_status(run_id, "completed")
        store.append_event(run_id, "apply_complete", {"gitlab": result})
        return {"status": "completed", "gitlab": result}
    finally:
        gitlab.close()
