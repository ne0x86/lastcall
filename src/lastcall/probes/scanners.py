"""Run the context pack's scanners on a clone (and, for osv-scanner, an extra folder of requirements files) and
print one JSON line per scanner:
{"tool": name, "ok": true|false, "data": its parsed output, "error": text}.

Runs inside the runner container, read-only on the clone (with its history, for gitleaks). Standard library only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", ".venv", "venv"}


def run(argv: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout, cwd="/tmp")
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout} s"
    except FileNotFoundError as e:
        return -1, "", str(e)
    return p.returncode, p.stdout, p.stderr


def emit(tool: str, data: Any = None, error: str | None = None) -> None:
    print(json.dumps({"tool": tool, "ok": error is None, "data": data, "error": error}), flush=True)


def gitleaks(work: Path) -> None:
    report = Path("/tmp/gitleaks.json")
    argv = ["gitleaks", "git", str(work), "--redact", "--no-banner", "--exit-code", "0"]
    code, out, err = run([*argv, "--report-format", "json", "--report-path", str(report)], 600)
    if code != 0 or not report.exists():
        emit("gitleaks", error=(err or out)[-500:] or f"exit code {code}")
        return
    emit("gitleaks", json.loads(report.read_text() or "[]"))


def osv(work: Path, extra: Path | None = None) -> None:
    dirs = [str(work)] + ([str(extra)] if extra and extra.is_dir() else [])
    code, out, err = run(["osv-scanner", "scan", "source", "-r", *dirs, "--format", "json"], 600)
    if code == 128:  # no package sources found
        emit("osv-scanner", {"results": []})
        return
    try:
        emit("osv-scanner", json.loads(out))  # exit code 1 only means vulnerabilities were found
    except ValueError:
        emit("osv-scanner", error=(err or out)[-500:] or f"exit code {code}")


def semgrep(work: Path) -> None:
    argv = ["semgrep", "scan", "--config", "p/default", "--json", "--quiet", "--metrics=off", str(work)]
    code, out, err = run(argv, 1200)
    try:
        emit("semgrep", json.loads(out))
    except ValueError:
        emit("semgrep", error=(err or out)[-500:] or f"exit code {code}")


def hadolint(work: Path) -> None:
    found: dict[str, Any] = {}
    files = [
        p for p in sorted(work.rglob("Dockerfile*")) if p.is_file() and not set(p.relative_to(work).parts) & SKIP_DIRS
    ]
    for path in files[:20]:
        code, out, err = run(["hadolint", "-f", "json", str(path)], 120)
        try:
            found[path.relative_to(work).as_posix()] = json.loads(out or "[]")
        except ValueError:
            found[path.relative_to(work).as_posix()] = {"error": (err or out)[-300:] or f"exit code {code}"}
    emit("hadolint", found)


def main(work: Path, extra: Path | None) -> None:
    gitleaks(work)
    osv(work, extra)
    semgrep(work)
    hadolint(work)


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/work"), Path(sys.argv[2]) if len(sys.argv) > 2 else None)
