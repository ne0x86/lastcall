"""Scanners for the context pack: gitleaks on the code and its history, osv-scanner on the dependency files,
semgrep's default rules, and hadolint on Dockerfiles. They run in the runner, read-only on the clone, through the
sandbox. Their output goes into the pack as leads that the reviewers confirm or dismiss against the code.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from lastcall import git
from lastcall.engines.base import CONTEXT_DIR, Invocation, json_line, probe_source
from lastcall.sandbox import Sandbox
from lastcall.verify import dockerfile_packages

SCAN_TIMEOUT = 40 * 60.0
TITLES = {
    "gitleaks": "Secret scanner (gitleaks, history included; values redacted)",
    "osv-scanner": "Dependencies with known vulnerabilities (osv-scanner)",
    "semgrep": "Static analysis (semgrep, default rules)",
    "hadolint": "Dockerfiles (hadolint)",
}


def scan(box: Sandbox, engine: str, clone: Path) -> dict[str, str]:
    """Pack sections from the scanners, run on the clone in the runner image of `engine`. Versions pinned only in a
    Dockerfile's pip install lines go to osv-scanner as an extra requirements file."""
    probe = f"{CONTEXT_DIR}/scanners.py"
    files = {probe: probe_source("scanners")}
    found = clone.rglob("Dockerfile*")
    dockerfiles = [p for p in found if ".git" not in p.parts and git.repo_file(clone, p.relative_to(clone))]
    pinned = sorted({p for d in dockerfiles for p in dockerfile_packages(d.read_text(errors="replace")) if "==" in p})
    if pinned:
        files[f"{CONTEXT_DIR}/deps/dockerfile-requirements.txt"] = "\n".join(pinned) + "\n"
    lines: list[str] = []
    invocation = Invocation(("python3", probe, "/work", f"{CONTEXT_DIR}/deps"), files=files)
    box.call(engine, invocation, lines.append, workspace=clone, timeout=SCAN_TIMEOUT)
    return sections({r["tool"]: r for line in lines if (r := json_line(line)) and "tool" in r})


def sections(results: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    out = {}
    for tool, title in TITLES.items():
        result = results.get(tool)
        if result is None:
            out[title] = f"{tool} did not run."
        elif not result.get("ok"):
            out[title] = f"{tool} failed: {result.get('error')}"
        else:
            out[title] = FORMAT[tool](result.get("data"))
    return out


def _gitleaks(data: Any) -> str:
    leaks = data if isinstance(data, list) else []
    if not leaks:
        return "No secrets found in the code or its history."
    lines = [f"{len(leaks)} possible secrets (values redacted):"]
    for leak in leaks[:20]:
        commit = str(leak.get("Commit") or "")[:8]
        lines.append(
            f"- {leak.get('RuleID')} in {leak.get('File')}:{leak.get('StartLine')}"
            + (f", commit {commit} ({str(leak.get('Date') or '')[:10]})" if commit else "")
        )
    return "\n".join(lines)


def _osv(data: Any) -> str:
    vulns = []
    for result in (data or {}).get("results") or []:
        for package in result.get("packages") or []:
            info = package.get("package") or {}
            for vuln in package.get("vulnerabilities") or []:
                severity = (vuln.get("database_specific") or {}).get("severity", "")
                fixed = sorted(
                    {
                        event["fixed"]
                        for affected in vuln.get("affected") or []
                        for rng in affected.get("ranges") or []
                        for event in rng.get("events") or []
                        if event.get("fixed")
                    }
                )
                vulns.append(
                    f"- {info.get('name')} {info.get('version')}: {vuln.get('id')} {severity} "
                    f"{str(vuln.get('summary') or '')[:120]} (fixed in: {', '.join(fixed[:3]) or 'unknown'})"
                )
    if not vulns:
        return "No known vulnerabilities in the dependency files."
    return f"{len(vulns)} known vulnerabilities:\n" + "\n".join(vulns[:40])


def _semgrep(data: Any) -> str:
    results = (data or {}).get("results") or []
    if not results:
        return "No findings."
    lines = [f"{len(results)} findings:"]
    for r in results[:40]:
        extra = r.get("extra") or {}
        path = str(r.get("path") or "").removeprefix("/work/")
        line = (r.get("start") or {}).get("line")
        rule = str(r.get("check_id") or "").rsplit(".", 1)[-1]
        lines.append(f"- {path}:{line} [{extra.get('severity')}] {rule}: {str(extra.get('message') or '')[:160]}")
    return "\n".join(lines)


def _hadolint(data: Any) -> str:
    files = data if isinstance(data, dict) else {}
    if not files:
        return "No Dockerfile."
    lines = []
    for name, items in files.items():
        if isinstance(items, dict):
            lines.append(f"- {name}: hadolint failed: {items.get('error')}")
        elif not items:
            lines.append(f"- {name}: no warnings")
        for item in items if isinstance(items, list) else []:
            lines.append(
                f"- {name}:{item.get('line')} {item.get('code')} [{item.get('level')}] {item.get('message')}"
            )
    return "\n".join(lines)


FORMAT: dict[str, Callable[[Any], str]] = {
    "gitleaks": _gitleaks,
    "osv-scanner": _osv,
    "semgrep": _semgrep,
    "hadolint": _hadolint,
}
