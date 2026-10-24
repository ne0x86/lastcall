"""A stand-in for `codex app-server`: answers initialize and account/rateLimits/read like the real server.

fake_codex.py                  answers with rate limits
fake_codex.py --not-logged-in  answers the rate limits request with an error
"""

import json
import sys

RATE_LIMITS = {
    "primary": {"usedPercent": 18.0, "windowDurationMins": 300, "resetsAt": 1_793_000_000},
    "secondary": {"usedPercent": 41.0, "windowDurationMins": 10_080, "resetsAt": 1_793_400_000},
    "credits": {"hasCredits": False, "unlimited": False, "balance": None},
    "spendControlReached": False,
}


def main() -> None:
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("id") == 0:
            reply: dict[str, object] = {"id": 0, "result": {"userAgent": "fake"}}
        elif msg.get("method") == "account/rateLimits/read":
            if "--not-logged-in" in sys.argv:
                reply = {"id": msg["id"], "error": {"code": -32600, "message": "not logged in"}}
            else:
                reply = {"id": msg["id"], "result": {"rateLimits": RATE_LIMITS}}
        else:
            continue
        print(json.dumps(reply))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
