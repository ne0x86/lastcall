"""Read the rate limits of a Codex login without calling the model.

Talks JSON-RPC to `codex app-server`: initialize, then account/rateLimits/read. Prints the answer (or the first
error) as one JSON line. Exit code 1 if the server ends without answering.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

COMMAND = ["codex", "app-server"]
CLIENT = {"name": "lastcall", "title": "lastcall", "version": "0"}


def main(command: list[str]) -> int:
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        send(proc, {"method": "initialize", "id": 0, "params": {"clientInfo": CLIENT}})
        assert proc.stdout is not None
        for line in proc.stdout:
            msg = parse(line)
            if msg.get("id") == 0 and "error" not in msg:
                send(proc, {"method": "initialized", "params": {}})
                send(proc, {"method": "account/rateLimits/read", "id": 1})
            elif msg.get("id") in (0, 1):
                print(json.dumps(msg), flush=True)
                return 0
        return 1
    finally:
        proc.kill()


def send(proc: subprocess.Popen[str], msg: dict[str, Any]) -> None:
    assert proc.stdin is not None
    try:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
    except OSError:  # the server already ended; its stderr says why
        pass


def parse(line: str) -> dict[str, Any]:
    try:
        msg = json.loads(line)
    except ValueError:
        return {}
    return msg if isinstance(msg, dict) else {}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or COMMAND))
