import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type RunStatus =
  | "pending"
  | "running"
  | "awaiting_approval"
  | "applying"
  | "completed"
  | "failed"
  | "cancelled";

interface SegmentRow {
  id: string;
  file_path: string;
  rationale: string;
  risk_level: string;
  original_content: string | null;
  new_content: string;
  approved: boolean;
  apply_status: string | null;
  apply_error: string | null;
}

interface RunPayload {
  run_id: string;
  status: RunStatus;
  goal: string;
  project_id: string;
  branch: string;
  setup_instructions: string | null;
  error_message: string | null;
  segments: SegmentRow[];
}

interface StreamPayload {
  id: number;
  ts: string;
  kind: string;
  payload: Record<string, unknown>;
}

function statusClass(s: RunStatus): string {
  if (s === "running" || s === "pending" || s === "applying") return "status-running";
  if (s === "awaiting_approval") return "status-awaiting";
  if (s === "completed") return "status-completed";
  return "status-failed";
}

export function App() {
  const [goal, setGoal] = useState("Validate my Python project setup");
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<RunPayload | null>(null);
  const [events, setEvents] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  const [editMap, setEditMap] = useState<Record<string, string>>({});
  const [approveMap, setApproveMap] = useState<Record<string, boolean>>({});

  const fetchRun = useCallback(async (id: string) => {
    const r = await fetch(`/api/runs/${id}`);
    if (!r.ok) return;
    const j = (await r.json()) as RunPayload;
    setRun(j);
    const em: Record<string, string> = {};
    const am: Record<string, boolean> = {};
    for (const s of j.segments) {
      em[s.id] = s.new_content;
      am[s.id] = s.approved;
    }
    setEditMap(em);
    setApproveMap(am);
  }, []);

  useEffect(() => {
    if (!runId) return;
    fetchRun(runId);
  }, [runId, fetchRun]);

  const startRun = async () => {
    setBusy(true);
    setEvents([]);
    try {
      const r = await fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal }),
      });
      if (!r.ok) {
        const t = await r.text();
        setEvents((e) => [...e, `Error: ${t}`]);
        return;
      }
      const j = await r.json();
      setRunId(j.run_id);
      setRun(null);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (!runId) return;
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
    const url = `/api/runs/${runId}/events?last_event_id=0`;
    const es = new EventSource(url);
    esRef.current = es;
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as StreamPayload;
        const summary = `[${data.kind}] ${JSON.stringify(data.payload).slice(0, 400)}`;
        setEvents((prev) => [...prev.slice(-200), summary]);
        if (data.kind === "phase1_complete" || data.payload?.message === "Awaiting human approval") {
          fetchRun(runId);
        }
      } catch {
        /* ignore */
      }
    };
    es.onerror = () => {
      es.close();
    };
    return () => {
      es.close();
    };
  }, [runId, fetchRun]);

  useEffect(() => {
    if (!runId || !run) return;
    if (run.status === "running" || run.status === "pending") {
      const t = setInterval(() => fetchRun(runId), 2000);
      return () => clearInterval(t);
    }
    return undefined;
  }, [runId, run?.status, fetchRun]);

  const segments = run?.segments ?? [];

  const submitApproval = async () => {
    if (!runId) return;
    setBusy(true);
    try {
      const body = {
        segments: segments.map((s) => {
          const draft = editMap[s.id] ?? s.new_content;
          return {
            id: s.id,
            approved: approveMap[s.id] ?? false,
            edited_content: draft !== s.new_content ? draft : undefined,
          };
        }),
      };
      const r = await fetch(`/api/runs/${runId}/approval`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const text = await r.text();
        setEvents((e) => [...e, text]);
      }
      await fetchRun(runId);
    } finally {
      setBusy(false);
    }
  };

  const applyChanges = async () => {
    if (!runId) return;
    setBusy(true);
    try {
      const r = await fetch(`/api/runs/${runId}/apply`, { method: "POST" });
      const t = await r.text();
      if (!r.ok) setEvents((e) => [...e, t]);
      else setEvents((e) => [...e, `apply: ${t}`]);
      await fetchRun(runId);
    } finally {
      setBusy(false);
    }
  };

  const instructions = useMemo(() => run?.setup_instructions ?? "", [run]);

  return (
    <div className="layout">
      <h1>Environment setup validation</h1>
      <p style={{ color: "var(--muted)", marginTop: 0 }}>
        GitLab discovery → static checks → ADK plan → you approve → commit to branch.
      </p>

      <div className="panel">
        <div className="row">
          <input
            type="text"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            placeholder="Goal / prompt"
          />
          <button type="button" className="primary" disabled={busy} onClick={startRun}>
            Start validation
          </button>
        </div>
        {runId && (
          <p style={{ marginTop: "0.75rem", fontSize: "0.85rem" }}>
            Run ID: <code>{runId}</code>
            {run && (
              <>
                {" "}
                <span className={`status-pill ${statusClass(run.status)}`}>{run.status}</span>
              </>
            )}
          </p>
        )}
      </div>

      <div className="panel">
        <h3 style={{ margin: "0 0 0.5rem", fontSize: "1rem" }}>Agent trace (SSE)</h3>
        <div className="events mono">
          {events.length === 0 ? (
            <div style={{ color: "var(--muted)" }}>Events appear when a run is active.</div>
          ) : (
            events.map((line, i) => (
              <div key={i} className="event-line">
                {line}
              </div>
            ))
          )}
        </div>
      </div>

      {instructions && (
        <div className="panel">
          <h3 style={{ margin: "0 0 0.5rem", fontSize: "1rem" }}>Setup instructions</h3>
          <pre
            style={{
              whiteSpace: "pre-wrap",
              margin: 0,
              fontSize: "0.85rem",
              color: "var(--muted)",
            }}
          >
            {instructions}
          </pre>
        </div>
      )}

      {segments.length > 0 && (
        <div className="panel">
          <h3 style={{ margin: "0 0 0.75rem", fontSize: "1rem" }}>
            Proposed changes (human review)
          </h3>
          {segments.map((s) => (
            <div key={s.id} className="segment">
              <h4>{s.file_path}</h4>
              <div className="meta">
                Risk: {s.risk_level}
                {s.apply_status && <> · Apply: {s.apply_status}</>}
                {s.apply_error && <> · {s.apply_error}</>}
              </div>
              <p style={{ fontSize: "0.88rem", margin: "0.35rem 0" }}>{s.rationale}</p>
              <label style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                <input
                  type="checkbox"
                  checked={approveMap[s.id] ?? false}
                  onChange={(e) =>
                    setApproveMap((m) => ({ ...m, [s.id]: e.target.checked }))
                  }
                />
                Approve for commit
              </label>
              <label style={{ display: "block", marginTop: "0.5rem", fontSize: "0.8rem" }}>
                Edited file body
              </label>
              <textarea
                className="full mono"
                value={editMap[s.id] ?? s.new_content}
                onChange={(e) => setEditMap((m) => ({ ...m, [s.id]: e.target.value }))}
              />
            </div>
          ))}
          <div className="row" style={{ marginTop: "0.5rem" }}>
            <button type="button" disabled={busy || run?.status !== "awaiting_approval"} onClick={submitApproval}>
              Save approval decisions
            </button>
            <button
              type="button"
              className="primary"
              disabled={busy || run?.status !== "awaiting_approval"}
              onClick={applyChanges}
            >
              Apply approved segments to GitLab
            </button>
          </div>
        </div>
      )}

      {run?.error_message && (
        <div className="panel" style={{ borderColor: "var(--danger)" }}>
          <strong>Error:</strong> {run.error_message}
        </div>
      )}
    </div>
  );
}
