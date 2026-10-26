"""The quickstart clones the stable branch, which always holds the latest release, and compose.yaml pulls the images
of its own release: a checkout of main between two releases still gets images that match its compose file."""

import re
from pathlib import Path

from lastcall import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_compose_readme_and_version_agree() -> None:
    compose = set(re.findall(r"\$\{LASTCALL_VERSION:-([^}]+)\}", (ROOT / "compose.yaml").read_text(encoding="utf-8")))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "git clone --branch stable https://github.com/ne0x86/lastcall " in readme  # the latest release, always
    assert not re.search(r"git clone --branch v\d", readme)
    assert len(compose) == 1, compose
    pinned = compose.pop()
    assert re.fullmatch(r"\d+\.\d+\.\d+", pinned), "pin a release, never latest or edge"
    if ".dev" not in __version__:  # a release commit pins itself
        assert pinned == __version__


def test_the_web_ui_listens_on_this_machine_unless_the_owner_says_otherwise() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert '"${LASTCALL_BIND:-127.0.0.1}:${LASTCALL_PORT:-8080}:8080"' in compose
