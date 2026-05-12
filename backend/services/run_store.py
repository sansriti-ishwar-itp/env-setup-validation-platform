"""SQLite persistence for validation runs, events, and segments."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Literal

logger = logging.getLogger(__name__)

RunStatus = Literal[
    "pending",
    "running",
    "awaiting_approval",
    "applying",
    "completed",
    "failed",
    "cancelled",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_log_path(run_id: str) -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    safe_run_id = "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in run_id)
    return repo_root / "logs" / f"agent-session-{safe_run_id}.jsonl"


@dataclass
class SegmentRecord:
    id: str
    file_path: str
    rationale: str
    risk_level: str
    original_content: str | None
    new_content: str
    approved: bool = False
    apply_status: str | None = None  # pending | applied | failed | skipped
    apply_error: str | None = None


@dataclass
class RunRecord:
    run_id: str
    status: RunStatus
    goal: str
    project_id: str
    branch: str
    created_at: str
    updated_at: str
    error_message: str | None = None
    setup_instructions: str | None = None
    segments: list[SegmentRecord] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class RunStore:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._lock = threading.Lock()
        Path(self._db_path()).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _db_path(self) -> str:
        if self._database_url.startswith("sqlite:///"):
            return self._database_url.replace("sqlite:///", "", 1)
        raise ValueError("Only sqlite:/// URLs are supported")

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._db_path(), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error_message TEXT,
                    setup_instructions TEXT,
                    meta_json TEXT,
                    segments_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id)"
            )

    def create_run(
        self,
        goal: str,
        project_id: str,
        branch: str,
        meta: dict[str, Any] | None = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        now = _utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, status, goal, project_id, branch,
                    created_at, updated_at, error_message, setup_instructions,
                    meta_json, segments_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    "pending",
                    goal,
                    project_id,
                    branch,
                    now,
                    now,
                    None,
                    None,
                    json.dumps(meta or {}),
                    json.dumps([]),
                ),
            )
        self._append_session_log(
            run_id,
            "run_created",
            {
                "status": "pending",
                "goal": goal,
                "project_id": project_id,
                "branch": branch,
                "metadata": meta or {},
            },
            ts=now,
        )
        return run_id

    def ensure_run(
        self,
        run_id: str,
        goal: str,
        project_id: str,
        branch: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        now = _utc_now()
        inserted = False
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO runs (
                    run_id, status, goal, project_id, branch,
                    created_at, updated_at, error_message, setup_instructions,
                    meta_json, segments_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    "running",
                    goal,
                    project_id,
                    branch,
                    now,
                    now,
                    None,
                    None,
                    json.dumps(meta or {}),
                    json.dumps([]),
                ),
            )
            inserted = cur.rowcount > 0
        if inserted:
            self._append_session_log(
                run_id,
                "run_created",
                {
                    "status": "running",
                    "goal": goal,
                    "project_id": project_id,
                    "branch": branch,
                    "metadata": meta or {},
                    "source": "adk_session",
                },
                ts=now,
            )

    def update_run_status(
        self,
        run_id: str,
        status: RunStatus,
        error_message: str | None = None,
    ) -> None:
        now = _utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE runs SET status = ?, updated_at = ?, error_message = ?
                WHERE run_id = ?
                """,
                (status, now, error_message, run_id),
            )
        self._append_session_log(
            run_id,
            "status_changed",
            {"status": status, "error_message": error_message},
            ts=now,
        )

    def save_analysis_result(
        self,
        run_id: str,
        setup_instructions: str,
        segments: list[SegmentRecord],
    ) -> None:
        now = _utc_now()
        payload = [self._segment_to_dict(s) for s in segments]
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE runs SET
                    status = ?,
                    updated_at = ?,
                    setup_instructions = ?,
                    segments_json = ?
                WHERE run_id = ?
                """,
                ("awaiting_approval", now, setup_instructions, json.dumps(payload), run_id),
            )
        self._append_session_log(
            run_id,
            "analysis_result_saved",
            {
                "status": "awaiting_approval",
                "setup_instructions": setup_instructions,
                "segments": payload,
            },
            ts=now,
        )

    def update_segments(self, run_id: str, segments: list[SegmentRecord]) -> None:
        now = _utc_now()
        payload = [self._segment_to_dict(s) for s in segments]
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE runs SET segments_json = ?, updated_at = ? WHERE run_id = ?",
                (json.dumps(payload), now, run_id),
            )
        self._append_session_log(
            run_id,
            "segments_updated",
            {"segments": payload},
            ts=now,
        )

    def append_event(self, run_id: str, kind: str, payload: dict[str, Any]) -> int:
        ts = _utc_now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO events (run_id, ts, kind, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, ts, kind, json.dumps(payload)),
            )
            event_id = int(cur.lastrowid)
        self._append_session_log(run_id, kind, payload, ts=ts, event_id=event_id)
        return event_id

    def recover_interrupted_runs(self) -> list[str]:
        """
        Mark runs left in non-terminal in-memory phases as failed after startup.
        SQLite keeps the state; the background asyncio task does not survive process restarts.
        """
        now = _utc_now()
        message = "Run was interrupted by a backend restart. Retry to continue from the same goal."
        interrupted: list[tuple[str, str, int]] = []
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, status FROM runs
                WHERE status IN ('pending', 'running', 'applying')
                """
            ).fetchall()
            for row in rows:
                run_id = str(row["run_id"])
                previous_status = str(row["status"])
                conn.execute(
                    """
                    UPDATE runs SET status = ?, updated_at = ?, error_message = ?
                    WHERE run_id = ?
                    """,
                    ("failed", now, message, run_id),
                )
                cur = conn.execute(
                    """
                    INSERT INTO events (run_id, ts, kind, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        now,
                        "run_interrupted",
                        json.dumps(
                            {
                                "message": message,
                                "previous_status": previous_status,
                            }
                        ),
                    ),
                )
                interrupted.append((run_id, previous_status, int(cur.lastrowid)))
        for run_id, previous_status, event_id in interrupted:
            self._append_session_log(
                run_id,
                "run_interrupted",
                {
                    "message": message,
                    "previous_status": previous_status,
                },
                ts=now,
                event_id=event_id,
            )
        return [run_id for run_id, _, _ in interrupted]

    def list_events_since(self, run_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, ts, kind, payload_json FROM events
                WHERE run_id = ? AND id > ?
                ORDER BY id ASC
                """,
                (run_id, after_id),
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r["id"],
                    "ts": r["ts"],
                    "kind": r["kind"],
                    "payload": json.loads(r["payload_json"]),
                }
            )
        return out

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        limit = max(1, min(limit, 200))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM runs
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_run(row)

    def _row_to_run(self, row: sqlite3.Row) -> RunRecord:
        segments_raw = json.loads(row["segments_json"] or "[]")
        segments = [self._dict_to_segment(s) for s in segments_raw]
        return RunRecord(
            run_id=row["run_id"],
            status=row["status"],  # type: ignore[arg-type]
            goal=row["goal"],
            project_id=row["project_id"],
            branch=row["branch"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            error_message=row["error_message"],
            setup_instructions=row["setup_instructions"],
            segments=segments,
            metadata=json.loads(row["meta_json"] or "{}"),
        )

    def _append_session_log(
        self,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        *,
        ts: str | None = None,
        event_id: int | None = None,
    ) -> None:
        """Mirror persisted agent session activity into repo-local JSONL logs."""
        record: dict[str, Any] = {
            "ts": ts or _utc_now(),
            "run_id": run_id,
            "kind": kind,
            "payload": payload,
        }
        if event_id is not None:
            record["event_id"] = event_id
        try:
            path = _session_log_path(run_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str))
                handle.write("\n")
        except OSError:
            logger.exception("Unable to write agent session log for run %s", run_id)

    def append_session_log(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        """Write a log-only session entry without adding a UI/database event."""
        self._append_session_log(run_id, kind, payload)

    @staticmethod
    def _segment_to_dict(s: SegmentRecord) -> dict[str, Any]:
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

    @staticmethod
    def _dict_to_segment(d: dict[str, Any]) -> SegmentRecord:
        return SegmentRecord(
            id=d["id"],
            file_path=d["file_path"],
            rationale=d.get("rationale", ""),
            risk_level=d.get("risk_level", "medium"),
            original_content=d.get("original_content"),
            new_content=d.get("new_content", ""),
            approved=bool(d.get("approved", False)),
            apply_status=d.get("apply_status"),
            apply_error=d.get("apply_error"),
        )
