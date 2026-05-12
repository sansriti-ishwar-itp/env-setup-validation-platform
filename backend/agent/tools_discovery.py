"""GitLab discovery tools for environment-related files."""

from __future__ import annotations

import logging
from typing import Any

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext

from .tool_runtime import STATE_FILE_CONTENTS, resolve_validation_runtime

logger = logging.getLogger(__name__)

NAME_HINTS = (
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    ".env.example",
    ".env.sample",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "Dockerfile",
    ".dockerignore",
    "README.md",
)

NAME_HINTS_LOWER = {hint.lower() for hint in NAME_HINTS}


def _matches_candidate(path: str) -> bool:
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    if name in NAME_HINTS_LOWER:
        return True
    if "/requirements/" in lower or lower.startswith("requirements/"):
        return True
    if name.startswith("requirements") and name.endswith(".txt"):
        return True
    return False


def discover_environment_files(
    max_files: int = 40,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """
    List and fetch environment-related files from the configured GitLab project/ref.
    Stores paths and contents in session state (evs_file_contents).
    """
    rt = resolve_validation_runtime(tool_context)
    tree = rt.gitlab.repository_tree(rt.project_id, rt.branch, recursive=True)
    paths: list[str] = []
    for node in tree:
        if node.get("type") != "blob":
            continue
        p = node.get("path") or ""
        if _matches_candidate(p):
            paths.append(p)
    paths = sorted(set(paths))[:max_files]

    contents: dict[str, str] = {}
    errors: dict[str, str] = {}

    def fetch_path(path: str, *, record_not_found: bool) -> None:
        try:
            raw = rt.gitlab.get_file_raw(rt.project_id, path, rt.branch)
            if raw is None:
                if record_not_found:
                    errors[path] = "not_found"
            elif len(raw) > 512 * 1024:
                contents[path] = raw[: 200 * 1024] + "\n... [truncated]\n"
            else:
                contents[path] = raw
        except Exception as exc:  # noqa: BLE001
            logger.exception("fetch %s", path)
            errors[path] = str(exc)

    for path in paths:
        fetch_path(path, record_not_found=True)

    # GitLab tree responses are paginated and can still be incomplete on older
    # servers/proxies. Probe common root setup files directly without surfacing
    # optional missing files as errors.
    for path in NAME_HINTS:
        if len(contents) + len(errors) >= max_files:
            break
        if path in contents or path in errors:
            continue
        fetch_path(path, record_not_found=False)

    paths = sorted(set(contents) | set(errors))

    rt.state[STATE_FILE_CONTENTS] = contents

    if rt.run_id:
        rt.store.append_event(
            rt.run_id,
            "discovery",
            {"paths": paths, "errors": errors, "fetched_count": len(contents)},
        )

    rt.gitlab.close()

    return {
        "discovered_paths": paths,
        "file_contents": contents,
        "fetch_errors": errors,
        "hint": "Call analyze_environment_setup next (uses session evs_file_contents).",
    }


def discover_environment_files_tool() -> FunctionTool:
    return FunctionTool(discover_environment_files)
