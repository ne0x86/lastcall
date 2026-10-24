"""Secrets that lastcall itself holds, such as the GitHub App key: one file per secret, readable only by lastcall's
user, written atomically. Subscription logins never come here: they stay in the CLIs' own storage (DESIGN.md,
section 3).

For now the files live in their own Docker volume with mode 0600; encryption at rest is still to come (section 12).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")


class SecretStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def get(self, name: str) -> str | None:
        path = self._path(name)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def set(self, name: str, value: str) -> None:
        path = self._path(name)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
        os.replace(tmp, path)  # a reader sees the old value or the new one, never half of it

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if not path.exists():
            return False
        path.unlink()
        return True

    def get_json(self, name: str) -> dict[str, Any] | None:
        raw = self.get(name)
        if raw is None:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"secret {name} is not a JSON object")
        return value

    def set_json(self, name: str, value: dict[str, Any]) -> None:
        self.set(name, json.dumps(value, indent=1))

    def _path(self, name: str) -> Path:
        if not NAME.fullmatch(name):
            raise ValueError(f"not a secret name: {name!r}")
        return self.root / f"{name}.secret"
