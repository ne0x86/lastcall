from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from lastcall.github import GitHub, GitHubError
from lastcall.quota import Reading, Window
from lastcall.recipes.audit import AuditReport
from lastcall.recipes.improve import ImproveReport
from lastcall.store import Store, import_runs
from lastcall.sync import ref, sync

REPO = "ne0x86/demo"


def finding(title: str) -> dict[str, Any]:
    return {
        "title": title,
        "severity": "high",
        "confidence": "high",
        "file": "a.py",
        "lines": "1",
        "evidence": "e",
        "why": "w",
        "fix": "f",
        "effort": "S",
        "fix_risk": "low",
        "verification": "v",
    }


def verdict(fid: str, decision: str, valid: bool = True) -> dict[str, Any]:
    return {
        "id": fid,
        "verdict": "valid" if valid else "false_positive",
        "duplicate_of": "",
        "severity": "high",
        "decision": decision,
        "reason": "r",
    }


def judged() -> AuditReport:
    r = AuditReport(REPO, "abc1234", "claude-code", "English")
    r.findings = [{"id": i, "reviewer": "bugs", **finding(f"finding {i}")} for i in ("B1", "B2", "S1", "H1")]
    r.verdicts = [verdict("B1", "pr"), verdict("B2", "pr"), verdict("S1", "issue"), verdict("H1", "discard", False)]
    r.prs = [{"title": "Fix B1", "ids": ["B1"], "risk": "low", "plan": "p"}]
    return r


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state" / "lastcall.db")


def test_accounts_and_projects(store: Store) -> None:
    store.add_account("claude", "claude-code", "Spanish", 15)
    store.add_account("claude", "claude-code", "English", 10)  # changing it keeps one row
    assert [(a.id, a.language, a.reserve_pct) for a in store.accounts()] == [("claude", "English", 10)]
    store.add_project(REPO, weight=2)
    store.add_project("ne0x86/off", active=False)
    assert [p.repo for p in store.projects(active_only=True)] == [REPO]
    assert store.project("NE0X86/DEMO") is not None


def test_readings_round_trip(store: Store) -> None:
    reading = Reading("claude", (Window("week", 54.0, 1_800_000_000.0, "expiring", 168.0),), False, "exact", 100.0)
    store.record_reading(reading)
    assert store.readings("claude", 0) == [reading]
    assert store.readings("claude", 200) == []


def test_findings_follow_their_story(store: Store) -> None:
    report = judged()
    store.record_findings("run1", report)
    assert store.statuses(REPO) == {"pending": 2, "decide": 1, "discarded": 1}
    report.deliveries = [
        {"title": "Fix B1", "ids": ["B1"], "status": "opened", "url": "https://github.com/ne0x86/demo/pull/5"}
    ]
    report.issue_url = "https://github.com/ne0x86/demo/issues/4"
    store.record_deliveries("run1", report)
    assert store.statuses(REPO) == {"in_pr": 1, "pending": 1, "decide": 1, "discarded": 1}
    assert [p["number"] for p in store.open_prs(REPO)] == [5]
    assert store.open_issues() == ["https://github.com/ne0x86/demo/issues/4"]

    earlier = {e["state"]: e for e in store.earlier(REPO)}
    assert set(earlier) == {"in_pr", "pending", "decide"}  # discarded ones are not remembered
    assert earlier["pending"]["fix"] == "f"  # pending ones carry everything: they may go into a pull request
    assert "fix" not in earlier["in_pr"]

    store.close_pr("https://github.com/ne0x86/demo/pull/5", merged=False)
    store.close_issue("https://github.com/ne0x86/demo/issues/4", completed=True)
    assert store.statuses(REPO) == {"rejected": 1, "pending": 1, "resolved": 1, "discarded": 1}


def test_a_delivery_can_carry_an_earlier_finding(store: Store) -> None:
    store.record_findings("run1", judged())
    pending = next(e for e in store.earlier(REPO) if e["state"] == "pending")
    later = AuditReport(REPO, "def5678", "claude-code", "English")
    later.deliveries = [
        {"title": "t", "ids": [pending["id"]], "status": "opened", "url": "https://github.com/ne0x86/demo/pull/9"}
    ]
    store.record_deliveries("run2", later)
    assert next(e for e in store.earlier(REPO) if e["id"] == pending["id"])["state"] == "in_pr"


def test_runs(store: Store) -> None:
    store.start_run("run1", REPO, "claude", "burst")
    assert store.last_run(REPO, "claude") is None  # still running
    store.finish_run("run1", "ok", "abc1234")
    store.start_run("run2", REPO, "claude", "burst")
    assert store.interrupt_running() == 1
    last = store.last_run(REPO, "claude")
    assert last is not None and last["sha"] == "abc1234"


def test_import_runs_once(store: Store) -> None:
    assert import_runs(store, [("run1", judged(), "claude")]) == 1
    assert import_runs(store, [("run1", judged(), "claude")]) == 0
    assert store.statuses(REPO)["pending"] == 2


class FakeGitHub:
    def __init__(self, answers: dict[str, dict[str, Any] | int]) -> None:
        self.answers = answers

    def repository_token(self, repo: str) -> str:
        return "ghs_x"

    def request(self, method: str, path: str, token: str, body: object = None) -> Any:
        answer = self.answers[path]
        if isinstance(answer, int):
            raise GitHubError(answer, "gone")
        return answer


def test_sync_reads_what_the_owner_did(store: Store) -> None:
    report = judged()
    store.record_findings("run1", report)
    report.deliveries = [
        {"title": "a", "ids": ["B1"], "status": "opened", "url": "https://github.com/ne0x86/demo/pull/5"},
        {"title": "b", "ids": ["B2"], "status": "opened", "url": "https://github.com/ne0x86/demo/pull/6"},
    ]
    report.issue_url = "https://github.com/ne0x86/demo/issues/4"
    store.record_deliveries("run1", report)
    fake = FakeGitHub(
        {
            "/repos/ne0x86/demo/pulls/5": {"state": "closed", "merged_at": "2026-10-24T20:00:00Z"},
            "/repos/ne0x86/demo/pulls/6": {"state": "open", "merged_at": None},
            "/repos/ne0x86/demo/issues/4": 404,  # deleted: what it held counts as dismissed
        }
    )
    messages: list[str] = []
    assert sync(store, cast(GitHub, fake), messages.append) == 2
    assert store.statuses(REPO) == {"merged": 1, "in_pr": 1, "dismissed": 1, "discarded": 1}
    assert len(messages) == 2


def test_ref() -> None:
    assert ref("https://github.com/ne0x86/demo/pull/12") == ("ne0x86/demo", 12)
    with pytest.raises(ValueError, match="not a GitHub"):
        ref("https://example.com/x")


def test_performance_findings_are_not_taken_for_earlier_ones(store: Store) -> None:
    report = judged()
    report.findings.append({"id": "P1", "reviewer": "performance", **finding("N+1 queries")})
    report.verdicts.append(verdict("P1", "pr"))
    store.record_findings("run1", report)
    assert store.statuses(REPO)["pending"] == 3


def test_a_database_from_before_v03_gets_the_crew_column(tmp_path: Path) -> None:
    import sqlite3

    old = sqlite3.connect(tmp_path / "old.db")
    old.execute(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, repo TEXT NOT NULL, account TEXT NOT NULL, trigger TEXT NOT NULL, "
        "status TEXT NOT NULL, sha TEXT, started REAL NOT NULL, ended REAL)"
    )
    old.execute("INSERT INTO runs VALUES ('r1', 'ne0x86/demo', 'claude', 'burst', 'ok', 'abc', 1.0, 2.0)")
    old.commit()
    old.close()
    store = Store(tmp_path / "old.db")
    assert store.runs()[0]["crew"] == ""
    assert store.runs()[0]["recipe"] == "audit"  # added in v0.4
    store.start_run("r2", "ne0x86/demo", "claude", "manual", ["chatgpt"])
    assert store.running()[0]["crew"] == "chatgpt"


def test_readings_keep_the_model_of_a_window(store: Store) -> None:
    opus = Window("week", 80.0, None, "expiring", 168.0, "Opus")
    store.record_reading(Reading("claude", (opus,), False, "exact", 100.0))
    assert store.readings("claude", 0)[0].windows == (opus,)
    old = '[{"name": "week", "used_pct": 5.0, "resets_at": null, "role": "expiring", "duration_hours": 168.0}]'
    store.db.execute("INSERT INTO readings VALUES ('codex', 50.0, ?, 0, 'exact')", (old,))  # saved by v0.3
    assert store.readings("codex", 0)[0].windows == (Window("week", 5.0, None, "expiring", 168.0),)


# ideas

PLAN = "https://github.com/ne0x86/demo/issues/7"


def improved(issue_url: str | None = PLAN) -> ImproveReport:
    r = ImproveReport(REPO, "abc1234", "claude-code", "English")
    r.ideas = [{"id": f"I{n}", "title": t, "kind": "feature", "change": "c"} for n, t in enumerate("ABC", 1)]
    r.verdicts = [{"id": "I1", "verdict": "keep"}, {"id": "I2", "verdict": "keep"}, {"id": "I3", "verdict": "vague"}]
    r.plan = ["I1", "I2"]
    r.issue_url = issue_url
    return r


def idea_states(store: Store) -> dict[str, str]:
    return {r["iid"]: r["status"] for r in store.q("SELECT iid, status FROM ideas ORDER BY id")}


def built(*deliveries: dict[str, Any]) -> ImproveReport:
    r = ImproveReport(REPO, "abc1234", "claude-code", "English")
    r.deliveries = list(deliveries)
    return r


def test_ideas_keep_their_story(store: Store) -> None:
    store.record_ideas("run1", improved())
    assert idea_states(store) == {"I1": "proposed", "I2": "proposed", "I3": "discarded"}
    assert store.plans() == [{"repo": REPO, "issue_url": PLAN, "n": 2}]
    assert [e["title"] for e in store.earlier_ideas(REPO)] == ["A", "B"]  # not what the critic left out
    assert store.tick_ideas(PLAN, {"I1": True, "I2": False}) == 1
    assert idea_states(store)["I1"] == "wanted" and store.wanted_count(REPO) == 1
    [wanted] = store.wanted_ideas(REPO)
    assert (wanted["id"], wanted["title"], wanted["plan"]) == ("I1", "A", PLAN)
    assert store.tick_ideas(PLAN, {"I1": False}) == 1  # unticked before it was built
    assert store.tick_ideas(PLAN, {"I1": True}) == 1
    url = "https://github.com/ne0x86/demo/pull/8"
    store.record_idea_deliveries("run2", built({"title": "A", "row": wanted["row"], "status": "opened", "url": url}))
    assert idea_states(store)["I1"] == "in_pr" and store.open_prs(REPO)[0]["number"] == 8
    store.close_pr(url, merged=True)
    assert idea_states(store)["I1"] == "merged"
    assert store.close_plan(PLAN, completed=True) == 1  # I2 was never ticked
    assert idea_states(store) == {"I1": "merged", "I2": "dismissed", "I3": "discarded"}
    assert store.plans() == [] and store.open_plans() == []


def test_an_idea_that_cannot_be_built_is_stuck(store: Store) -> None:
    store.record_ideas("run1", improved())
    store.tick_ideas(PLAN, {"I1": True, "I2": True})
    rows = {w["id"]: w["row"] for w in store.wanted_ideas(REPO)}
    store.record_idea_deliveries(
        "run2",
        built(
            {"title": "A", "row": rows["I1"], "status": "red", "note": "1 failed"},
            {"title": "B", "row": rows["I2"], "status": "stopped", "note": "5h window at 95%"},
        ),
    )
    assert idea_states(store) == {"I1": "stuck", "I2": "wanted", "I3": "discarded"}  # a gate only delays it
    assert store.close_plan(PLAN, completed=False) == 1  # closed as not planned: I2 is dropped too
    assert idea_states(store)["I2"] == "dismissed"


def test_an_unpublished_plan_leaves_nothing_to_tick(store: Store) -> None:
    store.record_ideas("run1", improved(issue_url=None))
    assert set(idea_states(store).values()) == {"discarded"}


def test_improvement_runs_are_not_audits(store: Store) -> None:
    store.start_run("r1", REPO, "claude", "manual", recipe="improve")
    store.finish_run("r1", "ok", "abc")
    assert store.last_run(REPO, "claude") is None  # the ranking counts audits
    assert store.runs()[0]["recipe"] == "improve"


def test_sync_reads_the_owners_ticks(store: Store) -> None:
    store.record_ideas("run1", improved())
    body = "- [x] **I1** A (x) <!-- lastcall:idea I1 -->\n- [ ] **I2** B (x) <!-- lastcall:idea I2 -->\n"
    fake = FakeGitHub({"/repos/ne0x86/demo/issues/7": {"state": "open", "body": body}})
    messages: list[str] = []
    assert sync(store, cast(GitHub, fake), messages.append) == 1
    assert idea_states(store)["I1"] == "wanted"
    fake.answers["/repos/ne0x86/demo/issues/7"] = {"state": "closed", "state_reason": "completed", "body": body}
    assert sync(store, cast(GitHub, fake), messages.append) == 1
    assert idea_states(store) == {"I1": "wanted", "I2": "dismissed", "I3": "discarded"}  # what was ticked stays


# turning a project on or off, and removing it

TICKET = "https://github.com/ne0x86/demo/issues/3"


def test_a_project_turns_on_and_off_and_nothing_else_of_it_changes(store: Store) -> None:
    store.add_project(REPO, weight=2, language="Spanish")
    store.update_project(
        REPO,
        active=True,
        weight=2,
        language="Spanish",
        max_prs=1,
        merge_on_approve=True,
        watch=True,
        health_url="https://example.com/health",
        tests=True,
        deps=True,
        signals="demo",
    )
    on = store.project(REPO)
    assert on is not None and on.active
    store.set_active(REPO, False)
    assert store.project(REPO) == replace(on, active=False)  # only the flag
    store.set_active(REPO, True)
    assert store.project(REPO) == on
    store.set_active(REPO, False, weight=0.5)  # a weight, when one is given
    assert store.project(REPO) == replace(on, active=False, weight=0.5)


def history(store: Store) -> None:
    """What a project leaves behind: a run, findings, a pull request, an improvement plan with an idea ticked, and a
    labelled issue that waits."""
    store.start_run("run1", REPO, "claude", "burst")
    store.finish_run("run1", "ok", "abc1234")
    report = judged()
    store.record_findings("run1", report)
    report.deliveries = [
        {"title": "Fix B1", "ids": ["B1"], "status": "opened", "url": "https://github.com/ne0x86/demo/pull/5"}
    ]
    store.record_deliveries("run1", report)
    store.close_pr("https://github.com/ne0x86/demo/pull/5", merged=False)  # the owner rejected it
    store.record_ideas("run1", improved())
    store.tick_ideas(PLAN, {"I1": True})
    store.sync_tickets(REPO, [{"number": 3, "url": TICKET, "title": "Make it faster"}])


def kept(store: Store) -> dict[str, Any]:
    """What the judge, the ranking and the pull requests of the project read from the store."""
    return {
        "runs": [r["id"] for r in store.runs()],
        "statuses": store.statuses(REPO),
        "earlier": store.earlier(REPO),
        "earlier_ideas": store.earlier_ideas(REPO),
        "wanted": store.wanted_count(REPO),
        "waiting": store.waiting_count(REPO),
        "prs": store.q("SELECT url, state FROM prs WHERE repo = ?", (REPO,)),
    }


def test_removing_a_project_keeps_all_its_history_and_adding_it_again_picks_it_up(store: Store) -> None:
    store.add_account("claude", "claude-code")
    store.add_project(REPO)
    store.add_project("ne0x86/other")
    history(store)
    before = kept(store)
    assert before["statuses"]["rejected"] == 1 and before["wanted"] == 1 and before["waiting"] == 1
    store.add_order("audit", REPO, None, 1.0)  # waits
    store.add_order("tests", REPO.upper(), "claude", 2.0)  # waits too, written in another case
    store.add_order("audit", "ne0x86/other", None, 3.0)  # another project's
    store.add_order("improve", REPO, None, 4.0)
    store.start_order(store.orders()[-1]["id"], 5.0)  # one that has started already
    assert store.remove_project(REPO) is True
    assert [p.repo for p in store.projects()] == ["ne0x86/other"] and store.project(REPO) is None
    assert [o["repo"] for o in store.orders()] == ["ne0x86/other"]  # what waited for it is cancelled
    assert [o["kind"] for o in store.q("SELECT kind FROM orders WHERE started IS NOT NULL")] == ["improve"]
    assert kept(store) == before  # and nothing else goes
    store.add_project(REPO)
    assert kept(store) == before  # the judge still knows what the owner rejected
    assert store.remove_project(REPO) is True  # a project that is not there: nothing to do
    assert store.remove_project("ne0x86/never-added") is True


def test_a_project_with_a_run_going_is_not_removed_and_nothing_changes(store: Store) -> None:
    store.add_project(REPO)
    store.add_order("audit", REPO, None, 1.0)
    store.start_run("run1", REPO.upper(), "claude", "manual")  # whatever the case
    assert store.remove_project(REPO) is False
    assert store.project(REPO) is not None and len(store.orders()) == 1
    store.finish_run("run1", "failed", None)
    assert store.remove_project(REPO) is True
    assert store.project(REPO) is None and store.orders() == []
    assert [r["id"] for r in store.runs()] == ["run1"]


def test_an_order_that_was_cancelled_cannot_start(store: Store) -> None:
    store.add_project(REPO)
    store.add_order("audit", REPO, None, 1.0)
    [order] = store.orders()
    assert store.remove_project(REPO)
    assert store.start_order(order["id"], 2.0) is False  # a service that still holds it does not run it
    store.add_order("audit", "ne0x86/other", None, 3.0)
    [waiting] = store.orders()
    assert store.start_order(waiting["id"], 4.0) is True
    assert store.start_order(waiting["id"], 5.0) is False  # started once
    assert store.orders() == []


def test_the_command_line_removes_a_project_by_the_same_rules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from lastcall import cli

    monkeypatch.setenv("LASTCALL_STATE", str(tmp_path / "state"))
    assert cli.main(["project", "add", REPO]) == 0 and cli.main(["project", "add", "ne0x86/other"]) == 0
    st = Store(tmp_path / "state" / "lastcall.db")
    st.add_order("audit", REPO, None, 1.0)
    st.start_run("run1", REPO, "claude", "manual")
    capsys.readouterr()
    assert cli.main(["project", "remove", REPO]) == 1  # a run is going on it: nothing changes
    assert "A run is going on ne0x86/demo: remove it when the run ends." in capsys.readouterr().err
    assert st.project(REPO) is not None and len(st.orders()) == 1
    st.finish_run("run1", "ok", "abc1234")
    assert cli.main(["project", "remove", "NE0X86/DEMO"]) == 0  # whatever the case
    assert "ne0x86/demo removed. Its history stays" in capsys.readouterr().out
    assert st.project(REPO) is None and st.orders() == [] and [r["id"] for r in st.runs()] == ["run1"]
    with pytest.raises(SystemExit):  # it is not a project any more
        cli.main(["project", "remove", REPO])
    assert "no project 'ne0x86/demo'" in capsys.readouterr().err
    assert cli.main(["project", "list"]) == 0
    listed = capsys.readouterr().out
    assert "ne0x86/other" in listed and REPO not in listed
    assert cli.main(["project", "add", REPO]) == 0  # and it can be added again
