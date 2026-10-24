import json
from pathlib import Path

from lastcall.secretscan import added_lines, scan
from lastcall.verify import check_command, describe, summary_line


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def test_python_with_tests_and_requirements(tmp_path: Path) -> None:
    root = write(tmp_path, {"app.py": "", "tests/test_app.py": "", "requirements.txt": "requests\n"})
    command, kind = check_command(root)
    assert kind == "tests"
    assert command.startswith(
        "pip install -q --user --no-warn-script-location -r requirements.txt pytest && python -m pytest"
    )


def test_python_package_with_dev_extras(tmp_path: Path) -> None:
    pyproject = '[project]\nname = "x"\n[project.optional-dependencies]\ndev = ["pytest"]\n'
    root = write(tmp_path, {"src/x/a.py": "", "test_a.py": "", "pyproject.toml": pyproject})
    assert check_command(root)[0].startswith("pip install -q --user --no-warn-script-location '.[dev]' pytest")


def test_python_without_tests_only_builds(tmp_path: Path) -> None:
    assert check_command(write(tmp_path, {"bot.py": ""})) == ("python -m compileall -q .", "build")


DOCKERFILE = """FROM python:3.11-slim
RUN pip install --no-cache-dir --upgrade pip && \\
    pip install --no-cache-dir "selenium==4.36.0" "pytz==2024.1"
RUN python -m pip install -r requirements.txt -e . && echo done
"""


def test_dependencies_declared_only_in_the_dockerfile(tmp_path: Path) -> None:
    root = write(tmp_path, {"Dockerfile": DOCKERFILE, "bot/bot.py": "", "bot/requirements-dev.txt": "black\n"})
    command, kind = check_command(root, assume_tests=True)
    assert kind == "tests"
    assert command == (
        "pip install -q --user --no-warn-script-location -r bot/requirements-dev.txt selenium==4.36.0 pytz==2024.1 "
        "pytest && "
        "python -m pytest -q -p no:cacheprovider"
    )
    assert check_command(root)[1] == "build"  # no tests yet


def test_package_names_cannot_inject_shell(tmp_path: Path) -> None:
    root = write(tmp_path, {"Dockerfile": 'RUN pip install "x$(touch /tmp/pwned)"\n', "a.py": "", "test_a.py": ""})
    assert "'x$(touch /tmp/pwned)'" in check_command(root)[0]  # quoted: the shell never runs it
    root = write(tmp_path, {"Dockerfile": 'RUN pip install "x;rm -rf /"\n'})
    assert "rm -rf" not in check_command(root)[0]  # a quote cut by the separator does not parse: dropped


def test_node_test_script(tmp_path: Path) -> None:
    root = write(tmp_path, {"package.json": json.dumps({"scripts": {"test": "vitest run"}})})
    assert check_command(root) == ("npm install --no-audit --no-fund --ignore-scripts && npm test", "tests")
    root = write(tmp_path, {"package.json": json.dumps({"scripts": {"test": "echo 'Error: no test specified'"}})})
    assert check_command(root) == ("true", "none")


def test_describe() -> None:
    assert describe("tests", "3 passed") == "tests run again in a clean container: `3 passed`"
    assert "only checked that the code compiles" in describe("build", "")


def test_secret_scan_names_rules_never_values() -> None:
    text = "key = 'AKIA" + "ABCDEFGHIJKLMNOP" + "'\n-----BEGIN RSA PRIVATE KEY-----\n"
    assert scan(text) == ["private key", "AWS access key"]
    assert scan("nothing secret here, sk-short") == []


def test_added_lines() -> None:
    diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new line\n context\n"
    assert added_lines(diff) == "new line"


def test_summary_line_prefers_the_test_summary() -> None:
    output = "collected 3 items\n3 passed in 0.42s\n  Consider adding this directory to PATH\n"
    assert summary_line(output) == "3 passed in 0.42s"
    assert summary_line("Tests:       2 passed, 2 total\nDone in 1s\n") == "Tests:       2 passed, 2 total"
    assert summary_line("just one line\n") == "just one line"
    assert summary_line("") == ""
