from lastcall.scanners import TITLES, sections

GITLEAKS = [
    {
        "RuleID": "github-pat",
        "File": "config.py",
        "StartLine": 3,
        "Commit": "0123456789ab",
        "Date": "2026-10-01T10:00:00Z",
    }
]
OSV = {
    "results": [
        {
            "packages": [
                {
                    "package": {"name": "requests", "version": "2.19.0"},
                    "vulnerabilities": [
                        {
                            "id": "GHSA-x84v-xcm2-53pg",
                            "summary": "Insufficiently protected credentials",
                            "database_specific": {"severity": "HIGH"},
                            "affected": [{"ranges": [{"events": [{"introduced": "0"}, {"fixed": "2.20.0"}]}]}],
                        }
                    ],
                }
            ]
        }
    ]
}
SEMGREP = {
    "results": [
        {
            "path": "/work/app.py",
            "start": {"line": 4},
            "check_id": "python.lang.security.audit.subprocess-shell-true.subprocess-shell-true",
            "extra": {"severity": "ERROR", "message": "Found 'subprocess' function with 'shell=True'."},
        }
    ]
}
HADOLINT = {"Dockerfile": [{"line": 1, "code": "DL3006", "level": "warning", "message": "Always tag the version"}]}


def run(tool: str, data: object) -> dict[str, object]:
    return {"tool": tool, "ok": True, "data": data, "error": None}


def test_sections_say_what_each_scanner_found() -> None:
    out = sections(
        {
            "gitleaks": run("gitleaks", GITLEAKS),
            "osv-scanner": run("osv-scanner", OSV),
            "semgrep": run("semgrep", SEMGREP),
            "hadolint": run("hadolint", HADOLINT),
        }
    )
    assert list(out) == list(TITLES.values())
    assert "- github-pat in config.py:3, commit 01234567 (2026-10-01)" in out[TITLES["gitleaks"]]
    assert "- requests 2.19.0: GHSA-x84v-xcm2-53pg HIGH" in out[TITLES["osv-scanner"]]
    assert "(fixed in: 2.20.0)" in out[TITLES["osv-scanner"]]
    assert "- app.py:4 [ERROR] subprocess-shell-true:" in out[TITLES["semgrep"]]
    assert "- Dockerfile:1 DL3006 [warning] Always tag the version" in out[TITLES["hadolint"]]


def test_clean_results_and_failures() -> None:
    out = sections(
        {
            "gitleaks": run("gitleaks", []),
            "osv-scanner": {"tool": "osv-scanner", "ok": False, "data": None, "error": "no network"},
            "hadolint": run("hadolint", {}),
        }
    )
    assert out[TITLES["gitleaks"]] == "No secrets found in the code or its history."
    assert out[TITLES["osv-scanner"]] == "osv-scanner failed: no network"
    assert out[TITLES["semgrep"]] == "semgrep did not run."
    assert out[TITLES["hadolint"]] == "No Dockerfile."
