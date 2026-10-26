"""Which project an account audits next, decided without any model call (DESIGN.md, section 8).

Left out: projects the same account audited in the last 7 days, unless the owner asked for something there (ideas
ticked in a plan, issues labelled `lastcall`); projects with 2 or more lastcall pull requests the owner has not
reviewed yet; and projects another account is working on right now.

Points: never audited by this account (100), or days since its last audit (2 a day, up to 30) and new commits since
the audited commit (3 each, up to 30); pending findings (5 each, up to 25); what the owner asked for (100, and 5 for
each ticked idea or labelled issue, up to 25: they go first); one unreviewed lastcall pull request (minus 20). The
project's weight multiplies the total.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from lastcall.store import ProjectRow

RECENT_DAYS = 7
MAX_OPEN_PRS = 2
DAY = 86_400.0


@dataclass(frozen=True)
class Candidate:
    repo: str
    score: float
    reasons: tuple[str, ...]


def rank(
    projects: Sequence[ProjectRow],
    *,
    last_run: Callable[[str], dict[str, Any] | None],
    open_prs: Callable[[str], int],
    pending: Callable[[str], int],
    new_commits: Callable[[str, str], int | None],
    busy: set[str],
    now: float,
    wanted: Callable[[str], int] = lambda repo: 0,
) -> list[Candidate]:
    found = []
    for p in projects:
        if not p.active or p.weight <= 0 or p.repo in busy:
            continue
        last, chosen = last_run(p.repo), wanted(p.repo)
        if last and now - float(last["started"]) < RECENT_DAYS * DAY and not chosen:
            continue
        unreviewed = open_prs(p.repo)
        if unreviewed >= MAX_OPEN_PRS:
            continue
        score, reasons = 0.0, []
        if last is None:
            score += 100
            reasons.append("never audited by this account")
        else:
            days = (now - float(last["started"])) / DAY
            score += min(30.0, 2 * days)
            reasons.append(f"{days:.0f} days since its last audit")
            commits = new_commits(p.repo, str(last["sha"])) if last.get("sha") else None
            if commits:
                score += min(30, 3 * commits)
                reasons.append(f"{commits} new commits")
        waiting = pending(p.repo)
        if waiting:
            score += min(25, 5 * waiting)
            reasons.append(f"{waiting} pending findings")
        if chosen:
            score += 100 + min(25, 5 * chosen)
            reasons.append(f"{chosen} asked for by the owner")
        if unreviewed == 1:
            score -= 20
            reasons.append("1 lastcall pull request to review")
        found.append(Candidate(p.repo, round(score * p.weight, 1), tuple(reasons)))
    return sorted(found, key=lambda c: (-c.score, c.repo))
