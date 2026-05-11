"""
Minimal GitLab REST client (read tree, raw files, multi-file commit).

Commit behaviour matches `push_files` in
`modular_agents/agents/gitlab_automation_agent/server.py` (actions create/update,
PRIVATE-TOKEN, projects/:id/repository/commits).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx
from httpx import HTTPStatusError

logger = logging.getLogger(__name__)


class GitLabClient:
    def __init__(
        self,
        token: str,
        api_url: str = "https://gitlab.com/api/v4",
        timeout: float = 60.0,
    ) -> None:
        self._api = api_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._client = httpx.Client(
            headers={
                "PRIVATE-TOKEN": token,
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def _project_path(self, project_id: str) -> str:
        return quote(str(project_id), safe="")

    def repository_tree(
        self,
        project_id: str,
        ref: str,
        path: str = "",
        recursive: bool = True,
    ) -> list[dict[str, Any]]:
        """Return flat list of tree nodes (blob/trtree)."""
        enc = self._project_path(project_id)
        url = f"{self._api}/projects/{enc}/repository/tree"
        params: dict[str, Any] = {"ref": ref, "recursive": recursive}
        if path:
            params["path"] = path
        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    def get_file_raw(self, project_id: str, file_path: str, ref: str) -> str | None:
        enc_proj = self._project_path(project_id)
        enc_path = quote(file_path, safe="")
        url = f"{self._api}/projects/{enc_proj}/repository/files/{enc_path}/raw"
        resp = self._client.get(url, params={"ref": ref})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.text

    def push_files(
        self,
        project_id: str,
        branch: str,
        files: list[dict[str, str]],
        commit_message: str,
    ) -> dict[str, Any]:
        """
        Commit multiple files in one request.
        Each item: {"path": "...", "content": "..."} (same as neurostack).
        """
        if not files:
            return {"error": "no files", "files_committed": []}

        enc = self._project_path(project_id)
        headers_json = {
            "PRIVATE-TOKEN": self._token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        existing: set[str] = set()
        for f in files:
            path = f.get("path") or f.get("file_path")
            if not path:
                continue
            try:
                check = self._client.get(
                    f"{self._api}/projects/{enc}/repository/files/{quote(path, safe='')}",
                    headers={"PRIVATE-TOKEN": self._token, "Accept": "application/json"},
                    params={"ref": branch},
                    timeout=30.0,
                )
                if check.status_code == 200:
                    existing.add(path)
            except HTTPStatusError:
                pass

        actions = []
        for f in files:
            path = f.get("path") or f.get("file_path")
            content = f.get("content")
            if not path or content is None:
                continue
            actions.append(
                {
                    "action": "update" if path in existing else "create",
                    "file_path": path,
                    "content": content if isinstance(content, str) else str(content),
                }
            )

        if not actions:
            return {"error": "no valid file entries", "files_committed": []}

        url = f"{self._api}/projects/{enc}/repository/commits"
        body = {
            "branch": branch,
            "commit_message": commit_message,
            "actions": actions,
        }
        resp = self._client.post(url, headers=headers_json, json=body)
        if resp.status_code != 201:
            return {
                "error": f"GitLab API {resp.status_code}: {resp.text}",
                "files_committed": [],
            }
        commit_data = resp.json()
        return {
            "commit_id": commit_data.get("id"),
            "commit_message": commit_message,
            "branch": branch,
            "files_committed": [a["file_path"] for a in actions],
        }
