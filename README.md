# Environment Setup Validation Platform

Python **FastAPI** platform with a GitLab-aware validation agent. It inspects Python/container environment artifacts (`requirements.txt`, `.env.example`, `docker-compose.yml`, `Dockerfile`, `README.md`, …), runs deterministic setup checks, proposes file-level fixes, and stops for **human approval** before committing to the configured branch. The app exposes both a project-owned Python API layer and an optional mounted Google ADK runtime surface.

## Use case

Platform engineers waste time on “works on my machine” failures that come from missing env vars, inconsistent compose ports, unpinned dependencies, and drift between Dockerfile and compose. This tool automates **discovery + analysis + patch proposals** while keeping **writes behind an explicit approval gate**, matching operational reality.

## Prerequisites

- Python **3.11+**
- Node **18+** (for the operator UI)
- Google ADK **1.32+** (`google-adk>=1.32.0`) for the managed agent runtime
- A **GitLab** personal access token with at least `read_api` and `write_repository`
- A **Gemini / Google AI** API key (`GOOGLE_API_KEY` or `GEMINI_API_KEY`, depending on your client defaults)

## Configuration

Copy `.env.example` to `.env` and set:

| Variable | Purpose |
|----------|---------|
| `GITLAB_PROJECT_ID` | Project ID or URL-encoded path (`group%2Fproject`) |
| `GITLAB_BRANCH` | Branch to read and commit to |
| `GITLAB_API_URL` | Default `https://gitlab.com/api/v4` |
| `GITLAB_PERSONAL_ACCESS_TOKEN` | Token (same naming style as neurostack SDLC agents) |
| `GEMINI_MODEL` | e.g. `gemini-2.0-flash` |
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | Model credentials |

Optional: `DATABASE_URL` (defaults to `sqlite:///…/data/runs.db` under this folder).

## Local setup

```bash
cd env_setup_validation_platform
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate       # macOS/Linux

pip install -e ".[dev]"
cd frontend
npm install
npm run dev                     # Vite on http://127.0.0.1:5173
```

In a **second** terminal:

```bash
cd env_setup_validation_platform
.venv\Scripts\activate
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

The UI proxies `/api` and `/health` to port **8000** (see `frontend/vite.config.ts`).

Optional ADK Web UI for direct agent testing:

```powershell
cd C:\Sans\env_setup_validation_platform
.\.venv\Scripts\activate
adk web adk_agents --port 8001
```

ADK Web uses the same `GITLAB_PROJECT_ID`, `GITLAB_BRANCH`, GitLab token, and Gemini key env vars. It creates local SQLite run state automatically when the backend FastAPI app is not running.

Windows shortcut (opens API in a new window, then runs Vite):

```powershell
.\scripts\dev.ps1
```

## Operator flow

1. Open the UI, leave or edit the goal (default: “Validate my Python project setup”), click **Initiate validation**.
2. Use **Recover or restart work** to reopen persisted runs by session id, filter successful/running/retryable runs, or retry failed/cancelled work.
3. Watch the live phase cards and **SSE trace** lines for tool calls, operator actions, and agent events.
4. When status becomes **awaiting approval**, review **setup instructions** and each **segment** (full-file replacement bodies).
5. Toggle **Approve this change for commit**, edit bodies if needed, click **Save decisions**.
6. Click **Apply approved changes** to create a single GitLab commit on `GITLAB_BRANCH`.

## API summary

### Operator API (this repo)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/runs` | List persisted runs for recovery/reopen UI |
| POST | `/api/runs` | Start analysis (`goal`, optional `project_id` / `branch`) |
| GET | `/api/runs/{id}` | Full run state + segments |
| GET | `/api/runs/{id}/events` | SSE event stream |
| POST | `/api/runs/{id}/approval` | Approve/reject/edit segments |
| POST | `/api/runs/{id}/interventions` | Add operator context while a run is active or awaiting approval |
| POST | `/api/runs/{id}/cancel` | Cancel a pending/running/awaiting-approval run |
| POST | `/api/runs/{id}/retry` | Restart a failed/cancelled run with the same goal/project/branch |
| POST | `/api/runs/{id}/apply` | Push approved segments via GitLab commits API |
| GET | `/api/agent/runtime` | Python-native runtime metadata and state keys |
| GET | `/api/agent/tools` | List direct Python tool execution capabilities |
| POST | `/api/agent/tools/execute` | Execute deterministic Python tools directly (`discover_environment_files`, `analyze_environment_setup`) |
| GET | `/api/agent/state/{run_id}` | Fetch persisted run state plus events through the Python API layer |
| GET | `/api/adk/endpoints` | Cheat sheet for mounted ADK routes |

### Python-native agent API

The assignment-facing API layer is under `/api/agent`. It exposes agent runtime metadata, deterministic tool execution, and persisted state without requiring callers to speak the ADK REST protocol directly. The managed `/api/runs` flow still uses the ADK-backed agent internally, but `/api/agent/tools/execute` can call the Python discovery/analysis tools directly with a `run_id`, `project_id`/`branch`, and optional tool arguments.

State keys used by the direct tool layer are:

- `evs_project_id` — GitLab project id or path.
- `evs_branch` — target branch/ref.
- `evs_run_id` — run/session id used to attach events to persisted state.
- `evs_file_contents` — discovered setup file contents used by analysis.

### ADK REST API (optional official surface, mounted at `/adk`)

The stock ADK FastAPI app from `google.adk.cli.fast_api.get_fast_api_app` is **mounted at `/adk`**, so you get the endpoints documented in the [ADK REST API Reference](https://google.github.io/adk-docs/api-reference/rest/), including:

- **`POST /adk/run_sse`** — streaming agent run (same protocol tools/dev harness use); **resume** by posting again with `invocationId` when using resumable flows.
- **`POST /adk/run`** — non-streaming run.
- **`GET /adk/list-apps`**, **`GET /adk/apps/{app_name}/...`** — sessions, artifacts, memory patch routes.
- **`GET /adk/debug/trace/{event_id}`**, **`GET /adk/debug/trace/session/{session_id}`** — traces / logs for debugging.

OpenAPI for the ADK app is typically at **`/adk/openapi.json`**.

Session state keys used by this agent’s tools (set automatically when you use `POST /api/runs`, or set manually when creating an ADK session): `evs_project_id`, `evs_branch`, `evs_run_id`, `evs_file_contents`.

The root agent is wrapped in **`google.adk.apps.App`** with **`ResumabilityConfig(is_resumable=True)`** in [`backend/validation_app.py`](backend/validation_app.py) so `/adk/run_sse` resume behaviour matches ADK docs.

**Session persistence:** set `ADK_SESSION_SERVICE_URI` (defaults to `sqlite:///…/data/adk_sessions.db`). `POST /api/runs` creates the session through the built-in ADK session endpoint, then runs through `/adk/run_sse`.

## State, failure handling, and restartability

- **State across steps:** SQLite stores run status, goal, GitLab target, setup instructions, proposed segments, approvals, apply status, and append-only events.
- **Graceful failures:** missing configuration, ADK/runtime errors, GitLab apply errors, cancelled runs, and unchanged approved content are surfaced as run statuses and timeline events.
- **Recovery after refresh:** `GET /api/runs` and `GET /api/runs/{id}` let the UI reopen previous sessions.
- **Restart/retry:** `POST /api/runs/{id}/retry` creates a new run from a failed/cancelled run using the same goal/project/branch.
- **Backend restart reconciliation:** startup marks stale `pending`, `running`, or `applying` runs as `failed` with a `run_interrupted` event so they do not stay stuck forever.

## Design choices

- **Two-phase orchestration**: Phase 1 is read-only on GitLab except metadata reads; phase 2 applies approved edits. This mirrors control-plane approval patterns without needing an external approval service.
- **Python-native API layer**: `/api/agent` exposes runtime metadata, direct tool execution, and state access even though the managed agent runtime remains ADK-backed.
- **REST GitLab client** instead of MCP/npx keeps local setup Python-only.
- **SQLite** stores runs, segments, and append-only events for transparency, refresh-safe UI, and restart recovery.
- **Neurostack alignment**: env vars mirror [`gitlab_utils.py`](../modular_agents/agents/sdlc_agents/gitlab_utils.py) and commit behavior follows [`gitlab_automation_agent/server.py`](../modular_agents/agents/gitlab_automation_agent/server.py) (`push_files`-style actions).

## Assignment checklist

- [ ] Meaningful **git commits** during development (not one giant commit).
- [ ] Paste **AI interaction logs** into [`docs/ai_transcript.md`](docs/ai_transcript.md) (or attach exported transcripts).
- [ ] Read [`ARCHITECTURE.md`](ARCHITECTURE.md) for diagrams and extension points.

## What we would build next

- Optional **merge request** instead of direct-to-branch commits.
- **pip-compile / uv lock** integration for dependency pinning suggestions.
- **Semgrep** or **bandit** hooks for security-adjacent checks without claiming full CVE coverage.
- **OAuth** for GitLab instead of PAT in `.env`.
