"""The quickstart clones a release, and compose.yaml pulls the images of that same release: a checkout of main between
two releases still gets images that match its compose file."""

import re
from pathlib import Path

from lastcall import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_compose_readme_and_version_agree() -> None:
    compose = set(re.findall(r"\$\{LASTCALL_VERSION:-([^}]+)\}", (ROOT / "compose.yaml").read_text(encoding="utf-8")))
    readme = set(re.findall(r"git clone --branch v(\S+) ", (ROOT / "README.md").read_text(encoding="utf-8")))
    assert len(compose) == 1 and compose == readme, (compose, readme)
    pinned = compose.pop()
    assert re.fullmatch(r"\d+\.\d+\.\d+", pinned), "pin a release, never latest or edge"
    if ".dev" not in __version__:  # a release commit pins itself
        assert pinned == __version__
