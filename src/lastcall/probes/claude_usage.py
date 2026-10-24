"""Read the plan usage from Claude Code without calling the model.

Starts a stream-json session, sends the initialize and get_usage control requests, and prints the get_usage control
response (or the first error) as one JSON line. Exit code 1 if the CLI ends without answering.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

COMMAND = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
USAGE_ID = "lastcall-usage"
REQUESTS: list[dict[str, Any]] = [
    {"type": "control_request", "request_id": "lastcall-init", "request": {"subtype": "initialize"}},
    {"type": "control_request", "request_id": USAGE_ID, "request": {"subtype": "get_usage", "skip_behaviors": True}},
]


def main(command: list[str]) -> int:
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    pending = list(REQUESTS)
    try:
        send(proc, pending.pop(0))
        assert proc.stdout is not None
        for line in proc.stdout:
            msg = parse(line)
            response = msg.get("response") if msg.get("type") == "control_response" else None
            if not isinstance(response, dict):
                continue
            if response.get("request_id") == USAGE_ID or response.get("subtype") == "error":
                print(json.dumps(msg), flush=True)
                return 0
            if pending:
                send(proc, pending.pop(0))
        return 1
    finally:
        proc.kill()


def send(proc: subprocess.Popen[str], msg: dict[str, Any]) -> None:
    assert proc.stdin is not None
    try:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
    except OSError:  # the CLI already ended; its stderr says why
        pass


def parse(line: str) -> dict[str, Any]:
    try:
        msg = json.loads(line)
    except ValueError:
        return {}
    return msg if isinstance(msg, dict) else {}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or COMMAND))
