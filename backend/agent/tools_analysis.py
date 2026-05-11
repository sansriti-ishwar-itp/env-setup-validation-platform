"""Deterministic environment setup checks."""

from __future__ import annotations

import json
import re
from typing import Any

import yaml
from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext

from .tool_runtime import file_contents_dict, resolve_validation_runtime

try:
    from yaml import CSafeLoader as YLoader
except ImportError:
    from yaml import SafeLoader as YLoader  # type: ignore[misc]


def _parse_ports_compose(data: dict[str, Any]) -> dict[str, list[str]]:
    ports_by_service: dict[str, list[str]] = {}
    services = data.get("services") or {}
    if not isinstance(services, dict):
        return ports_by_service
    for sname, svc in services.items():
        if not isinstance(svc, dict):
            continue
        raw_ports = svc.get("ports")
        if not raw_ports:
            continue
        plist: list[str] = []
        if isinstance(raw_ports, list):
            for p in raw_ports:
                plist.append(str(p))
        elif isinstance(raw_ports, str):
            plist.append(raw_ports)
        ports_by_service[sname] = plist
    return ports_by_service


def _extract_expose_dockerfile(content: str) -> list[str]:
    ports: list[str] = []
    for line in content.splitlines():
        m = re.match(r"^\s*EXPOSE\s+(.+)$", line, re.I)
        if m:
            parts = m.group(1).split()
            ports.extend(parts)
    return ports


def _scan_requirements(content: str) -> dict[str, Any]:
    unpinned: list[str] = []
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # strip extras for name
        base = re.split(r"[<>=!~\[]", s, 1)[0].strip()
        if not base:
            continue
        if "==" not in s and "~=" not in s and "@" not in s:
            unpinned.append(s)
    return {"unpinned_lines": unpinned[:50], "unpinned_count": len(unpinned)}


def _env_keys_from_example(content: str) -> list[str]:
    keys: list[str] = []
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" in s:
            k = s.split("=", 1)[0].strip()
            if k:
                keys.append(k)
    return keys


def analyze_environment_setup(
    file_contents_json: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """
    Run static checks on files in session state evs_file_contents (or pass JSON map).
    Returns findings and draft_segments the LLM can refine before finalize_validation_plan.
    """
    rt = resolve_validation_runtime(tool_context)
    contents: dict[str, Any] = dict(file_contents_dict(rt))
    if file_contents_json.strip():
        try:
            contents = json.loads(file_contents_json)
        except json.JSONDecodeError:
            pass

    findings: list[dict[str, Any]] = []
    draft_segments: list[dict[str, Any]] = []

    compose_text = None
    compose_path = None
    for path, text in contents.items():
        if "docker-compose" in path.lower() or path.endswith("compose.yml") or path.endswith(
            "compose.yaml"
        ):
            compose_text = text
            compose_path = path
            break

    dockerfile_text = None
    dockerfile_path = None
    for path, text in contents.items():
        if path.endswith("Dockerfile") or path.endswith("/Dockerfile"):
            dockerfile_text = text
            dockerfile_path = path
            break

    req_text = None
    req_path = None
    for path, text in contents.items():
        if path.endswith("requirements.txt") or "/requirements/" in path.lower():
            req_text = text
            req_path = path
            break

    env_example = None
    env_path = None
    for path, text in contents.items():
        if path.endswith(".env.example") or path.endswith(".env.sample"):
            env_example = text
            env_path = path
            break

    if compose_text:
        try:
            data = yaml.load(compose_text, Loader=YLoader)
            if isinstance(data, dict):
                pb = _parse_ports_compose(data)
                for svc, plist in pb.items():
                    findings.append(
                        {
                            "severity": "info",
                            "category": "compose",
                            "message": f"Service '{svc}' publishes ports: {plist}",
                        }
                    )
                # duplicate host port detection
                host_ports: dict[str, list[str]] = {}
                for svc, plist in pb.items():
                    for entry in plist:
                        entry_s = str(entry)
                        if ":" in entry_s:
                            host = entry_s.split(":", 1)[0]
                            host_ports.setdefault(host, []).append(svc)
                for hp, svcs in host_ports.items():
                    if hp.isdigit() and len(svcs) > 1:
                        findings.append(
                            {
                                "severity": "warning",
                                "category": "ports",
                                "message": f"Host port {hp} referenced by multiple services: {svcs}",
                            }
                        )
        except Exception as exc:  # noqa: BLE001
            findings.append(
                {
                    "severity": "error",
                    "category": "compose",
                    "message": f"Could not parse YAML for {compose_path}: {exc}",
                }
            )

    if dockerfile_text:
        exposed = _extract_expose_dockerfile(dockerfile_text)
        if exposed:
            findings.append(
                {
                    "severity": "info",
                    "category": "dockerfile",
                    "message": f"EXPOSE directives: {exposed}",
                }
            )

    if req_text and req_path:
        scan = _scan_requirements(req_text)
        if scan["unpinned_count"]:
            findings.append(
                {
                    "severity": "warning",
                    "category": "dependencies",
                    "message": f"{scan['unpinned_count']} requirement lines appear unpinned "
                    f"(no == or ~=). Examples: {scan['unpinned_lines'][:5]}",
                }
            )

    if env_example and env_path:
        keys = _env_keys_from_example(env_example)
        findings.append(
            {
                "severity": "info",
                "category": "env",
                "message": f".env template defines {len(keys)} keys (sample): {keys[:15]}",
            }
        )

    if compose_text and env_example and compose_path and env_path:
        # shallow check: DATABASE_URL in example but not referenced in compose env for any service
        try:
            data = yaml.load(compose_text, Loader=YLoader)
            if isinstance(data, dict):
                services = data.get("services") or {}
                referenced: set[str] = set()
                if isinstance(services, dict):
                    for svc in services.values():
                        if isinstance(svc, dict):
                            for k in (svc.get("environment") or {}):
                                if isinstance(k, str):
                                    referenced.add(k)
                            env_list = svc.get("environment")
                            if isinstance(env_list, list):
                                for item in env_list:
                                    if isinstance(item, str) and "=" in item:
                                        referenced.add(item.split("=", 1)[0].strip())
                missing = [k for k in _env_keys_from_example(env_example) if k not in referenced]
                if missing:
                    findings.append(
                        {
                            "severity": "warning",
                            "category": "config_gap",
                            "message": f"Keys present in {env_path} but not mapped in "
                            f"{compose_path} service env: {missing[:20]}",
                        }
                    )
        except Exception:
            pass

    if rt.run_id:
        rt.store.append_event(
            rt.run_id,
            "analysis",
            {"findings_count": len(findings), "draft_segments": len(draft_segments)},
        )

    rt.gitlab.close()

    return {
        "findings": findings,
        "draft_segments": draft_segments,
        "note": "Merge these with LLM reasoning; call finalize_validation_plan with final segments.",
    }


def analyze_environment_setup_tool() -> FunctionTool:
    return FunctionTool(analyze_environment_setup)
