"""A stand-in for an OpenAI-compatible model server, for the opencode test in the sandbox: the step starts it next
to opencode. Its model first asks to read hello.txt, then answers with the JSON of a review. It touches
/tmp/stand-in-ready once it listens. Usage: python3 stand_in_provider.py <port>
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ANSWER = '{"summary": "nothing to report", "findings": []}'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.send_json({"object": "list", "data": [{"id": "stand-in", "object": "model"}]})

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        messages = request.get("messages") or []
        if request.get("tools") and not any(m.get("role") == "tool" for m in messages):
            call = {"index": 0, "id": "call_1", "type": "function"}
            call["function"] = {"name": "read", "arguments": json.dumps({"filePath": "/work/hello.txt"})}
            self.chunk({"role": "assistant", "content": None, "tool_calls": [call]})
            self.chunk({}, "tool_calls")
        else:
            self.chunk({"role": "assistant", "content": ""})
            self.chunk({"content": ANSWER})
            self.chunk({}, "stop")
        self.chunk(None, usage={"prompt_tokens": 1234, "completion_tokens": 56, "total_tokens": 1290})
        self.wfile.write(b"data: [DONE]\n\n")

    def chunk(self, delta: dict[str, object] | None, finish: str | None = None, usage: object = None) -> None:
        choices = [{"index": 0, "delta": delta, "finish_reason": finish}] if delta is not None else []
        data = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()), "model": "stand-in"}
        data |= {"choices": choices, **({"usage": usage} if usage else {})}
        self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()

    def send_json(self, value: object) -> None:
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler)
Path("/tmp/stand-in-ready").touch()
server.serve_forever()
