"""One pull request, built the same way for every recipe: the implementer's change in its own clone, checked by
lastcall outside the agent, in the project's environment, with one more attempt when the check fails; then the
secret scan, the commit, the push and the pull request.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from lastcall import git
from lastcall.environment import Environment
from lastcall.recipes import prompt, render
from lastcall.recipes.steps import Steps
from lastcall.secretscan import added_lines, scan
from lastcall.verify import Kind, check_command, describe, summary_line

ATTEMPTS = 2  # an implementer whose change fails the check gets the failure and one more attempt
Verifier = Callable[[Path, str], tuple[bool, str]]  # (the change, check command) -> (passed, last lines of output)


class Forge(Protocol):
    @property
    def author(self) -> tuple[str, str]: ...  # name and email of the commits

    def push(self, workdir: Path, branch: str) -> None: ...

    def open_pull_request(self, branch: str, title: str, body: str) -> str: ...

    def open_issue(self, title: str, body: str) -> str: ...


@dataclass(frozen=True)
class Job:
    """One pull request to build: what the implementer is asked, and what the commit and the pull request say."""

    title: str
    ask: Callable[[str, str], str]  # (branch, how the change is checked) -> the implementer's prompt
    trailer: str  # the commit message's last line, such as "Findings: B1, S2"
    footer: Callable[[str, str, str | None], str]  # (what was checked, model, effort) -> the pull request's footer
    allowed: Callable[[str], bool] | None = None  # the paths that the change may touch; None: any


def slug(title: str, size: int = 40) -> str:
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:size].rstrip("-") or "change"


def build(
    job: Job,
    base: Mapping[str, Any],
    deliveries: list[dict[str, Any]],
    *,
    n: int,
    stamp: str,
    steps: Steps,
    run_dir: Path,
    tiers: Mapping[str, tuple[str, str | None]],
    environment: Environment,
    verify: Verifier,
    forge: Forge,
    language: str,
) -> dict[str, Any]:
    """Build the job's pull request in `run_dir/pr<n>`. Its record (`base` and the branch) is in `deliveries` from
    the start, in place of one with the same title, and ends with `status`: `opened` with its `url`, or why not
    (`stopped` by a gate, `not_done`, `unchanged`, `outside` what the job may touch, `red`, `secrets`) with a
    `note`."""
    branch = f"lastcall/{stamp}-{n}-{slug(job.title)}"
    work = run_dir / f"pr{n}"
    git.remove(work)
    git.branch_from(run_dir / "clone", work, branch)
    command = environment.test or check_command(work)[0]
    with_tests = environment.test or check_command(work, assume_tests=True)[0]  # once tests exist
    text = job.ask(branch, check_text(environment, command, with_tests))
    record: dict[str, Any] = {**base, "branch": branch}
    deliveries[:] = [d for d in deliveries if d["title"] != job.title] + [record]
    asked, passed, tail, diff = text, False, "", ""
    out: dict[str, Any] = {}
    kind: Kind = "none"
    for attempt in range(1, ATTEMPTS + 1):
        role = "implementer" if attempt == 1 else "implementer:retry"
        res = steps.run(role, "deep", asked, "implementation", work, writable=True)
        if res is None:  # a gate closed: the rest waits for the next run
            record |= {"status": "stopped", "note": steps.report.stopped}
            return record
        out = res.output or {}
        if not res.ok or not out.get("done"):
            record |= {"status": "not_done", "note": str(out.get("pending") or res.error or "")[:800]}
            return record
        diff = git.stage_all(work)
        if not diff.strip():
            record |= {"status": "unchanged", "note": "the implementer reported done but changed nothing"}
            return record
        outside = [path for path in git.staged_paths(work) if job.allowed and not job.allowed(path)]
        if outside:
            record |= {"status": "outside", "note": f"it changed what it may not: {', '.join(outside[:10])}"}
            return record
        command, kind = check_command(work)  # the implementer may have added the project's first tests
        if environment.test:
            command, kind = environment.test, "tests"
        passed, tail = verify(work, command)
        record["attempts"] = attempt
        if passed:
            record.pop("status", None)  # what the failed attempt left
            record.pop("note", None)
            break
        record |= {"status": "red", "note": tail[-1500:]}
        asked = text + retry_text(environment, command, tail)
    if not passed:
        return record
    title, body = str(out["pr_title"]), str(out["pr_body"])
    leaks = scan(f"{added_lines(diff)}\n{title}\n{body}")
    if leaks:
        record |= {"status": "secrets", "note": f"blocked by the secret scan: {', '.join(leaks)}"}
        return record
    model, effort = tiers["deep"]
    git.commit(work, f"{title}\n\n{out.get('summary') or ''}\n\n{job.trailer}", forge.author)
    forge.push(work, branch)
    checked = describe(kind, summary_line(tail), language, environment.image)
    footer = "\n\n---\n<sub>" + job.footer(checked, model, effort) + "</sub>"
    record |= {"status": "opened", "url": forge.open_pull_request(branch, title, body + footer), "check": checked}
    return record


def retry_text(environment: Environment, command: str, tail: str) -> str:
    """What the implementer's second attempt adds to its prompt: the check that failed, and how."""
    where = f"in the project's environment (`{environment.image}`)" if environment.image else "in a clean container"
    return render(prompt("retry.md"), command=command, where=where, tail=tail[-3000:].strip())


def check_text(environment: Environment, command: str, with_tests: str) -> str:
    """Step 4 of the implementer's prompt: how its change is checked."""
    if environment.image is None:
        return (
            f"Run the whole check with: `{command}`. It must pass. If something already failed before your change, "
            "do not fix it: say so in `pending`. Then lastcall checks your change again, in a clean container "
            "where only the project's declared dependencies are installed. If you add tests, it runs them with "
            f"`{with_tests}`: make sure they pass exactly that way, and do not rely on anything you installed by "
            "hand."
        )
    return (
        f"The project's checks run in its own environment, the image `{environment.image}` "
        f"({environment.source}), which this container does not have: you cannot run them here. When you finish, "
        f"lastcall prepares that environment and runs `{command}` in a clean container. Write the change and its "
        "tests so that they pass that way: read the existing tests and follow them closely. In `tests.result`, "
        f"write that the tests run in {environment.image} after you finish."
    )
