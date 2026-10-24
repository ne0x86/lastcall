"""A stand-in for `claude -p --input-format stream-json`: answers control requests like the real CLI.

fake_claude.py                 answers initialize and get_usage
fake_claude.py --not-logged-in answers initialize with an error
fake_claude.py --silent        ends without answering
"""

import json
import sys

USAGE = {
    "subscription_type": "team",
    "rate_limits_available": True,
    "rate_limits": {
        "five_hour": {"utilization": 12, "resets_at": "2026-10-24T20:00:00+00:00"},
        "seven_day": {"utilization": 36, "resets_at": "2026-10-26T12:00:00+00:00"},
        "seven_day_opus": {"utilization": 10, "resets_at": "2026-10-26T12:00:00+00:00"},
    },
    "extra_usage": {"is_enabled": False, "monthly_limit": None, "used_credits": None, "utilization": None},
}


def answer(request_id: str, subtype: str, **rest: object) -> None:
    print(
        json.dumps({"type": "control_response", "response": {"subtype": subtype, "request_id": request_id, **rest}})
    )
    sys.stdout.flush()


def main() -> None:
    if "--silent" in sys.argv:
        return
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("type") != "control_request":
            continue
        kind = (msg.get("request") or {}).get("subtype")
        if "--not-logged-in" in sys.argv:
            answer(msg["request_id"], "error", error="Not logged in. Run /login")
        elif kind == "initialize":
            answer(msg["request_id"], "success", response={"commands": []})
        elif kind == "get_usage":
            answer(msg["request_id"], "success", response=USAGE)


if __name__ == "__main__":
    main()
