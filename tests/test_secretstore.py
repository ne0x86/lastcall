import sys
from pathlib import Path

import pytest

from lastcall.secretstore import SecretStore


def test_round_trip(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets")
    assert store.get("github-app") is None
    store.set_json("github-app", {"id": 1})
    assert store.get_json("github-app") == {"id": 1}
    store.set("github-app", "replaced")
    assert store.get("github-app") == "replaced"


@pytest.mark.parametrize("name", ["../x", "a/b", "Upper", "", ".hidden"])
def test_names_stay_inside_the_store(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="not a secret name"):
        SecretStore(tmp_path).set(name, "x")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_only_lastcall_can_read_a_secret(tmp_path: Path) -> None:
    SecretStore(tmp_path).set("key", "x")
    assert (tmp_path / "key.secret").stat().st_mode & 0o777 == 0o600
