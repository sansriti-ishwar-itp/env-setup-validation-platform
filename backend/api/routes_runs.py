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
from ..services.run_executor import (
    cancel_analysis_task,
    spawn_analysis_task,
    validate_apply_prerequisites,
)
from ..services.run_store import RunRecord, RunStore, SegmentRecord
from .models import (
    ApprovalBody,
    CancelRunBody,
    CreateRunBody,
    CreateRunResponse,
    InterventionBody,
    RunSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runs", tags=["runs"])


def _store(request: Request) -> RunStore:
    return request.app.state.store


@router.get("", response_model=list[RunSummary])
async def list_runs(request: Request, limit: int = 50) -> list[RunSummary]:
    store = _store(request)
    return [_run_summary(run) for run in store.list_runs(limit=limit)]


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
    return _run_summary(run)


@router.post("/{run_id}/retry", response_model=CreateRunResponse)
async def retry_run(run_id: str, request: Request) -> CreateRunResponse:
    store = _store(request)
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, detail="Run not found")
    if run.status not in ("failed", "cancelled"):
        raise HTTPException(
            409,
            detail=f"Only failed or cancelled runs can be retried (status={run.status})",
        )

    new_run_id = store.create_run(
        goal=run.goal,
        project_id=run.project_id,
        branch=run.branch,
        meta={"source": "retry", "retry_of": run.run_id},
    )
    store.append_event(
        run.run_id,
        "retry_started",
        {"new_run_id": new_run_id, "message": "Retry created from this run."},
    )
    store.append_event(
        new_run_id,
        "retry_of",
        {"source_run_id": run.run_id, "message": "Run restarted from a saved run."},
    )
    spawn_analysis_task(
        run_id=new_run_id,
        goal=run.goal,
        store=store,
        project_id=run.project_id,
        branch=run.branch,
        adk_app=request.app.state.adk_app,
    )
    return CreateRunResponse(run_id=new_run_id)


def _run_summary(run: RunRecord) -> RunSummary:
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


@router.post("/{run_id}/interventions")
async def add_intervention(
    run_id: str,
    body: InterventionBody,
    request: Request,
) -> dict[str, Any]:
    store = _store(request)
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, detail="Run not found")
    if run.status in ("completed", "failed", "cancelled", "applying"):
        raise HTTPException(400, detail=f"Run cannot accept interventions (status={run.status})")

    event_payload = {"message": body.message, "status_at_submit": run.status}
    event_id = store.append_event(run_id, "operator_intervention", event_payload)
    event = store.list_events_since(run_id, event_id - 1)[0]
    return {"status": "ok", "event": event}


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    body: CancelRunBody,
    request: Request,
) -> dict[str, Any]:
    store = _store(request)
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, detail="Run not found")
    if run.status in ("completed", "failed", "cancelled"):
        raise HTTPException(400, detail=f"Run already finished (status={run.status})")
    if run.status == "applying":
        raise HTTPException(400, detail="Cannot cancel while GitLab apply is in progress")

    task_cancelled = cancel_analysis_task(run_id)
    reason = body.reason or "Cancelled by operator"
    store.update_run_status(run_id, "cancelled")
    event_payload = {
        "message": reason,
        "task_cancelled": task_cancelled,
        "status_at_cancel": run.status,
    }
    event_id = store.append_event(run_id, "run_cancelled", event_payload)
    event = store.list_events_since(run_id, event_id - 1)[0]
    return {"status": "cancelled", "event": event}


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

    updated_segments: list[SegmentRecord] = []
    commit_files: list[dict[str, str]] = []
    unchanged_files: list[str] = []
    gitlab = GitLabClient(token=token, api_url=api_url)

    try:
        for seg in run.segments:
            if not seg.approved:
                seg.apply_status = "skipped"
                seg.apply_error = None
                updated_segments.append(seg)
                continue

            remote_content = gitlab.get_file_raw(run.project_id, seg.file_path, run.branch)
            if remote_content == seg.new_content:
                seg.apply_status = "unchanged"
                seg.apply_error = "Approved content already matches the target branch"
                unchanged_files.append(seg.file_path)
            else:
                seg.apply_status = "pending"
                seg.apply_error = None
                commit_files.append({"path": seg.file_path, "content": seg.new_content})
            updated_segments.append(seg)

        store.update_segments(run_id, updated_segments)
        if not commit_files:
            store.append_event(
                run_id,
                "apply_skipped",
                {
                    "message": "No approved segments changed the target branch content.",
                    "unchanged_files": unchanged_files,
                },
            )
            raise HTTPException(
                400,
                detail="Approved segments do not change any GitLab files. Edit a proposed body "
                "or approve a segment with an actual code delta before applying.",
            )

        store.update_run_status(run_id, "applying")
        store.append_event(
            run_id,
            "apply_started",
            {
                "files": [f["path"] for f in commit_files],
                "unchanged_files": unchanged_files,
            },
        )
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
            if seg.apply_status == "pending":
                seg.apply_status = "applied"
        store.update_segments(run_id, updated_segments)
        store.update_run_status(run_id, "completed")
        store.append_event(run_id, "apply_complete", {"gitlab": result})
        return {"status": "completed", "gitlab": result}
    finally:
        gitlab.close()
