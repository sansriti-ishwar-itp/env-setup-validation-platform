import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type RunStatus =
  | "pending"
  | "running"
  | "awaiting_approval"
  | "applying"
  | "completed"
  | "failed"
  | "cancelled";

type EventCategory = "all" | "agent" | "tool" | "operator" | "system";
type StepState = "done" | "active" | "waiting";

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
  created_at: string;
  updated_at: string;
  setup_instructions: string | null;
  error_message: string | null;
  segments: SegmentRow[];
}

interface StreamEvent {
  id: number;
  ts: string;
  kind: string;
  payload: Record<string, unknown>;
}

interface ProgressStep {
  label: string;
  description: string;
  state: StepState;
}

const FINAL_STATUSES: RunStatus[] = ["completed", "failed", "cancelled"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function valueToString(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return null;
}

function shorten(text: string, max = 220): string {
  const compact = text.replace(/\s+/g, " ").trim();
  if (compact.length <= max) return compact;
  return `${compact.slice(0, max - 1)}...`;
}

function formatTime(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(d);
}

function statusClass(status: RunStatus): string {
  if (status === "running" || status === "pending" || status === "applying") {
    return "status-running";
  }
  if (status === "awaiting_approval") return "status-awaiting";
  if (status === "completed") return "status-completed";
  if (status === "cancelled") return "status-cancelled";
  return "status-failed";
}

function namesFromPayload(payload: Record<string, unknown>, key: string): string[] {
  const entries = payload[key];
  if (!Array.isArray(entries)) return [];
  return entries
    .map((entry) => {
      if (!isRecord(entry)) return null;
      return valueToString(entry.name) ?? valueToString(entry.id);
    })
    .filter((name): name is string => Boolean(name));
}

function textFromPayload(payload: Record<string, unknown>): string {
  const text = payload.text;
  if (Array.isArray(text)) {
    return text.map(valueToString).filter((v): v is string => Boolean(v)).join("\n");
  }
  return valueToString(payload.message) ?? valueToString(payload.raw) ?? "";
}

function eventCategory(event: StreamEvent): EventCategory {
  if (event.kind === "adk_event") {
    const calls = namesFromPayload(event.payload, "function_calls");
    const responses = namesFromPayload(event.payload, "function_responses");
    return calls.length || responses.length ? "tool" : "agent";
  }
  if (
    ["analysis", "finalize", "apply_started", "apply_complete", "apply_failed", "apply_skipped"].includes(
      event.kind,
    )
  ) {
    return "tool";
  }
  if (["approval", "operator_intervention", "run_cancelled"].includes(event.kind)) {
    return "operator";
  }
  return "system";
}

function eventTitle(event: StreamEvent): string {
  const payload = event.payload;
  if (event.kind === "run_started") return "Agent run started";
  if (event.kind === "analysis") {
    return `Static analysis found ${valueToString(payload.findings_count) ?? "0"} findings`;
  }
  if (event.kind === "finalize") {
    return `Agent proposed ${valueToString(payload.segment_count) ?? "0"} file changes`;
  }
  if (event.kind === "phase1_complete") return "Waiting for human review";
  if (event.kind === "approval") return "Approval decisions saved";
  if (event.kind === "operator_intervention") return "Operator note added";
  if (event.kind === "run_cancelled") return "Run cancelled by operator";
  if (event.kind === "apply_started") return "Applying approved changes";
  if (event.kind === "apply_skipped") return "No GitLab changes to apply";
  if (event.kind === "apply_complete") return "GitLab commit completed";
  if (event.kind === "apply_failed") return "GitLab apply failed";
  if (event.kind === "error") return "Agent error";

  if (event.kind === "adk_event") {
    const calls = namesFromPayload(payload, "function_calls");
    const responses = namesFromPayload(payload, "function_responses");
    if (calls.length) return `Agent chose tool: ${calls.join(", ")}`;
    if (responses.length) return `Tool returned: ${responses.join(", ")}`;
    const text = textFromPayload(payload);
    if (text) return "Agent reasoning update";
  }
  return event.kind.replaceAll("_", " ");
}

function eventDetail(event: StreamEvent): string {
  const payload = event.payload;
  if (event.kind === "run_started") {
    return valueToString(payload.goal) ?? "The agent is preparing the validation session.";
  }
  if (event.kind === "analysis") {
    const drafts = valueToString(payload.draft_segments) ?? "0";
    return `${drafts} draft segments were produced by deterministic checks.`;
  }
  if (event.kind === "approval") {
    const updated = payload.updated;
    return Array.isArray(updated) ? `Updated ${updated.length} review decisions.` : "";
  }
  if (event.kind === "operator_intervention") {
    return valueToString(payload.message) ?? "";
  }
  const text = textFromPayload(payload);
  if (text) return shorten(text, 480);
  const json = JSON.stringify(payload);
  return json === "{}" ? "" : shorten(json, 480);
}

function buildProgressSteps(run: RunPayload | null, events: StreamEvent[]): ProgressStep[] {
  const hasRun = Boolean(run);
  const hasAnalysis = events.some((event) => event.kind === "analysis");
  const hasFinalPlan = Boolean(run?.segments.length) || events.some((event) => event.kind === "finalize");
  const isReviewing = run?.status === "awaiting_approval";
  const isApplying = run?.status === "applying";
  const isComplete = run?.status === "completed";
  const isStopped = run ? FINAL_STATUSES.includes(run.status) : false;

  return [
    {
      label: "Initiate",
      description: "Create a tracked validation run.",
      state: hasRun ? "done" : "active",
    },
    {
      label: "Observe",
      description: "Watch GitLab discovery, checks, and tool calls stream in.",
      state: hasAnalysis || hasFinalPlan || isReviewing || isStopped ? "done" : hasRun ? "active" : "waiting",
    },
    {
      label: "Understand",
      description: "Review the agent's visible choices and proposed plan.",
      state: hasFinalPlan || isReviewing || isStopped ? "done" : hasAnalysis ? "active" : "waiting",
    },
    {
      label: "Intervene",
      description: "Add notes, cancel, or edit approval decisions before writes.",
      state: isReviewing ? "active" : hasFinalPlan || isStopped || isApplying ? "done" : "waiting",
    },
    {
      label: "Apply",
      description: "Commit only the changes you approve.",
      state: isComplete ? "done" : isApplying ? "active" : "waiting",
    },
  ];
}

function isInterventionAllowed(status: RunStatus | undefined): boolean {
  return status === "pending" || status === "running" || status === "awaiting_approval";
}

function segmentHasDelta(segment: SegmentRow, draft: string): boolean {
  return segment.original_content === null || draft !== segment.original_content;
}

export function App() {
  const [goal, setGoal] = useState("Validate my Python project setup");
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<RunPayload | null>(null);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [interventionText, setInterventionText] = useState("");
  const [eventFilter, setEventFilter] = useState<EventCategory>("all");
  const esRef = useRef<EventSource | null>(null);
  const localEventId = useRef(-1);

  const [editMap, setEditMap] = useState<Record<string, string>>({});
  const [approveMap, setApproveMap] = useState<Record<string, boolean>>({});

  const appendEvent = useCallback((event: StreamEvent) => {
    setEvents((prev) => {
      if (prev.some((existing) => existing.id === event.id)) return prev;
      return [...prev.slice(-300), event];
    });
  }, []);

  const appendLocalEvent = useCallback(
    (kind: string, message: string) => {
      appendEvent({
        id: localEventId.current--,
        ts: new Date().toISOString(),
        kind,
        payload: { message },
      });
    },
    [appendEvent],
  );

  const fetchRun = useCallback(async (id: string) => {
    const r = await fetch(`/api/runs/${id}`);
    if (!r.ok) return;
    const j = (await r.json()) as RunPayload;
    setRun(j);
    setEditMap((prev) => {
      const next: Record<string, string> = {};
      for (const segment of j.segments) {
        next[segment.id] = prev[segment.id] ?? segment.new_content;
      }
      return next;
    });
    setApproveMap((prev) => {
      const next: Record<string, boolean> = {};
      for (const segment of j.segments) {
        next[segment.id] = prev[segment.id] ?? segment.approved;
      }
      return next;
    });
  }, []);

  useEffect(() => {
    if (!runId) return;
    fetchRun(runId);
  }, [runId, fetchRun]);

  const startRun = async () => {
    setBusy(true);
    setEvents([]);
    setRun(null);
    setRunId(null);
    setEditMap({});
    setApproveMap({});
    try {
      const r = await fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal }),
      });
      if (!r.ok) {
        const text = await r.text();
        appendLocalEvent("error", text);
        return;
      }
      const j = (await r.json()) as { run_id: string };
      setRunId(j.run_id);
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
    const es = new EventSource(`/api/runs/${runId}/events?last_event_id=0`);
    esRef.current = es;
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as StreamEvent;
        appendEvent(data);
        if (
          data.kind !== "adk_event" ||
          data.payload.message === "Awaiting human approval"
        ) {
          fetchRun(runId);
        }
      } catch {
        appendLocalEvent("error", "Could not parse a server-sent event from the run stream.");
      }
    };
    es.onerror = () => {
      es.close();
    };
    return () => {
      es.close();
    };
  }, [runId, appendEvent, appendLocalEvent, fetchRun]);

  useEffect(() => {
    if (!runId || !run) return;
    if (run.status === "running" || run.status === "pending" || run.status === "applying") {
      const t = setInterval(() => fetchRun(runId), 2000);
      return () => clearInterval(t);
    }
    return undefined;
  }, [runId, run, fetchRun]);

  const segments = run?.segments ?? [];
  const approvedCount = segments.filter((segment) => approveMap[segment.id]).length;
  const approvedChangeCount = segments.filter((segment) => {
    const draft = editMap[segment.id] ?? segment.new_content;
    return approveMap[segment.id] && segmentHasDelta(segment, draft);
  }).length;
  const instructions = useMemo(() => run?.setup_instructions ?? "", [run]);
  const steps = useMemo(() => buildProgressSteps(run, events), [run, events]);
  const decisionEvents = useMemo(
    () =>
      events.filter((event) =>
        ["agent", "tool", "operator"].includes(eventCategory(event)),
      ),
    [events],
  );
  const visibleEvents = useMemo(
    () =>
      eventFilter === "all"
        ? events
        : events.filter((event) => eventCategory(event) === eventFilter),
    [events, eventFilter],
  );

  const submitApproval = async () => {
    if (!runId) return;
    setBusy(true);
    try {
      const body = {
        segments: segments.map((segment) => {
          const draft = editMap[segment.id] ?? segment.new_content;
          return {
            id: segment.id,
            approved: approveMap[segment.id] ?? false,
            edited_content: draft !== segment.new_content ? draft : undefined,
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
        appendLocalEvent("error", text);
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
      const text = await r.text();
      if (!r.ok) appendLocalEvent("error", text);
      await fetchRun(runId);
    } finally {
      setBusy(false);
    }
  };

  const submitIntervention = async () => {
    if (!runId || !interventionText.trim()) return;
    setBusy(true);
    try {
      const r = await fetch(`/api/runs/${runId}/interventions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: interventionText.trim() }),
      });
      const payload = await r.json();
      if (!r.ok) {
        appendLocalEvent("error", JSON.stringify(payload));
        return;
      }
      if (isRecord(payload.event)) appendEvent(payload.event as unknown as StreamEvent);
      setInterventionText("");
      await fetchRun(runId);
    } finally {
      setBusy(false);
    }
  };

  const cancelRun = async () => {
    if (!runId) return;
    setBusy(true);
    try {
      const r = await fetch(`/api/runs/${runId}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: interventionText.trim() || "Cancelled from operator UI" }),
      });
      const payload = await r.json();
      if (!r.ok) {
        appendLocalEvent("error", JSON.stringify(payload));
        return;
      }
      if (isRecord(payload.event)) appendEvent(payload.event as unknown as StreamEvent);
      await fetchRun(runId);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="layout">
      <header className="hero">
        <div>
          <p className="eyebrow">Human-in-the-loop validation</p>
          <h1>Environment setup validation</h1>
          <p className="hero-copy">
            Start a task, watch the agent work in real time, inspect its visible decisions,
            and step in before anything is written to GitLab.
          </p>
        </div>
        {run && <span className={`status-pill ${statusClass(run.status)}`}>{run.status}</span>}
      </header>

      <section className="panel command-panel">
        <div className="section-heading">
          <div>
            <h2>1. Initiate the task</h2>
            <p>Describe the validation objective. Repository details stay server-side.</p>
          </div>
          <button type="button" className="primary big-action" disabled={busy} onClick={startRun}>
            {run ? "Start another run" : "Initiate validation"}
          </button>
        </div>
        <textarea
          className="prompt-box"
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          placeholder="Tell the agent what to validate..."
        />
      </section>

      <section className="progress-grid">
        {steps.map((step) => (
          <div key={step.label} className={`step-card step-${step.state}`}>
            <span className="step-dot" />
            <strong>{step.label}</strong>
            <p>{step.description}</p>
          </div>
        ))}
      </section>

      <main className="dashboard-grid">
        <section className="panel">
          <div className="section-heading compact">
            <div>
              <h2>2. Observe real-time progress</h2>
              <p>Structured events from the run stream are grouped by intent.</p>
            </div>
            <select
              value={eventFilter}
              onChange={(e) => setEventFilter(e.target.value as EventCategory)}
            >
              <option value="all">All events</option>
              <option value="agent">Agent updates</option>
              <option value="tool">Tool decisions</option>
              <option value="operator">Operator actions</option>
              <option value="system">System</option>
            </select>
          </div>
          <div className="events">
            {visibleEvents.length === 0 ? (
              <div className="empty-state">Events appear here as soon as a run is active.</div>
            ) : (
              visibleEvents.map((event) => (
                <article key={event.id} className={`event-line event-${eventCategory(event)}`}>
                  <div className="event-topline">
                    <span>{eventTitle(event)}</span>
                    <time>{formatTime(event.ts)}</time>
                  </div>
                  {eventDetail(event) && <p>{eventDetail(event)}</p>}
                </article>
              ))
            )}
          </div>
        </section>

        <aside className="panel">
          <div className="section-heading compact">
            <div>
              <h2>3. Decisions and intervention</h2>
              <p>Visible choices, tool calls, and your mid-run controls.</p>
            </div>
          </div>
          <div className="decision-list">
            {decisionEvents.length === 0 ? (
              <div className="empty-state">Decision cards appear when the agent calls tools.</div>
            ) : (
              decisionEvents.slice(-5).reverse().map((event) => (
                <article key={event.id} className="decision-card">
                  <span className={`category-pill category-${eventCategory(event)}`}>
                    {eventCategory(event)}
                  </span>
                  <h3>{eventTitle(event)}</h3>
                  {eventDetail(event) && <p>{eventDetail(event)}</p>}
                </article>
              ))
            )}
          </div>
          <div className="intervention-box">
            <label>
              Operator note
              <textarea
                value={interventionText}
                onChange={(e) => setInterventionText(e.target.value)}
                placeholder="Add context, ask the reviewer to consider something, or explain why you are cancelling..."
              />
            </label>
            <div className="row">
              <button
                type="button"
                disabled={busy || !runId || !interventionText.trim() || !isInterventionAllowed(run?.status)}
                onClick={submitIntervention}
              >
                Send note
              </button>
              <button
                type="button"
                className="danger"
                disabled={busy || !runId || !isInterventionAllowed(run?.status)}
                onClick={cancelRun}
              >
                Cancel run
              </button>
            </div>
            <p className="help-text">
              Notes are saved to the timeline. To redirect active analysis, cancel and start a
              new run with the updated objective.
            </p>
          </div>
        </aside>
      </main>

      {instructions && (
        <section className="panel">
          <div className="section-heading compact">
            <div>
              <h2>Setup instructions</h2>
              <p>The agent's operator-facing guidance before applying changes.</p>
            </div>
          </div>
          <pre className="instruction-block">{instructions}</pre>
        </section>
      )}

      {segments.length > 0 && (
        <section className="panel">
          <div className="section-heading">
            <div>
              <h2>4. Human review</h2>
              <p>
                {approvedCount} of {segments.length} proposed changes selected.{" "}
                {approvedChangeCount} selected changes have a code delta.
              </p>
            </div>
            <div className="row">
              <button
                type="button"
                disabled={busy || run?.status !== "awaiting_approval"}
                onClick={submitApproval}
              >
                Save decisions
              </button>
              <button
                type="button"
                className="primary"
                disabled={busy || run?.status !== "awaiting_approval" || approvedChangeCount === 0}
                onClick={applyChanges}
              >
                Apply approved changes
              </button>
            </div>
          </div>
          <div className="segment-list">
            {segments.map((segment) => (
              <article key={segment.id} className="segment">
                <div className="segment-header">
                  <div>
                    <h3>{segment.file_path}</h3>
                    <p>{segment.rationale || "No rationale provided."}</p>
                  </div>
                  <div className="segment-badges">
                    <span className={`risk-pill risk-${segment.risk_level.toLowerCase()}`}>
                      {segment.risk_level} risk
                    </span>
                    {!segmentHasDelta(segment, editMap[segment.id] ?? segment.new_content) && (
                      <span className="risk-pill risk-unchanged">No code delta</span>
                    )}
                  </div>
                </div>
                <div className="meta">
                  Apply status: {segment.apply_status ?? "not applied"}
                  {segment.apply_error && <> · {segment.apply_error}</>}
                </div>
                <label className="approval-toggle">
                  <input
                    type="checkbox"
                    checked={approveMap[segment.id] ?? false}
                    onChange={(e) =>
                      setApproveMap((m) => ({ ...m, [segment.id]: e.target.checked }))
                    }
                  />
                  Approve this change for commit
                </label>
                {segment.original_content && (
                  <details className="original-content">
                    <summary>View original file body</summary>
                    <pre>{segment.original_content}</pre>
                  </details>
                )}
                <label className="edit-label">
                  Editable proposed file body
                  <textarea
                    className="full mono"
                    value={editMap[segment.id] ?? segment.new_content}
                    onChange={(e) =>
                      setEditMap((m) => ({ ...m, [segment.id]: e.target.value }))
                    }
                  />
                </label>
              </article>
            ))}
          </div>
        </section>
      )}

      {run?.error_message && (
        <section className="panel error-panel">
          <strong>Error:</strong> {run.error_message}
        </section>
      )}
    </div>
  );
}
