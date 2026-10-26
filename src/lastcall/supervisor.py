"""The runner supervisor: the only component with the Docker socket. See DESIGN.md, section 5.

It listens on a Unix socket and runs each request in a fresh runner container. A request names an engine, a workspace
and the files the step needs. The supervisor validates every path and always applies the same isolation:
unprivileged user, no capabilities, resource limits, and the internal network whose only way out is the egress
proxy. A step that verifies a change may name a project's environment image instead of the runner's; such a step
never gets an account volume.

Data volume layout: `runs/` belongs to the service (uid 1000, like the runners) and holds workspaces and shared
files; `steps/<id>/ctx/` is written here for one step and mounted read-only at /ctx.

Response: newline-delimited JSON, `{"out": line}` for each line the CLI prints, then
`{"exit": code, "timed_out": bool, "stderr": tail}`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socketserver
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path, PurePosixPath
from typing import Any

from lastcall import engines
from lastcall.environment import IMAGE
from lastcall.process import stream

log = logging.getLogger("lastcall.supervisor")

RUNNER_UID = 1000
MAX_BODY = 32 * 1024 * 1024
MAX_TIMEOUT = 2 * 3600.0
SAFE_PATH = re.compile(r"[A-Za-z0-9._/-]+")  # no commas: paths end up in --mount specs
ACCOUNT_VOLUME = re.compile(r"lastcall-account-[a-z0-9][a-z0-9-]{0,39}")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
RESERVED_ENV = {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"}
NO_PROXY = "localhost,127.0.0.1,ollama"  # the local model server lives on the sandbox network, not behind the proxy


@dataclass(frozen=True)
class Config:
    socket: Path = Path("/run/lastcall/supervisor.sock")
    data_root: Path = Path("/data")
    data_volume: str = "lastcall-data"
    network: str = "lastcall-sandbox"
    proxy: str = "http://egress:8888"
    images: Mapping[str, str] = field(default_factory=lambda: dict.fromkeys(engines.BUILTIN, "lastcall-runner:dev"))
    client_gid: int = 1000
    memory: str = "3g"
    cpus: str = "2"
    pids: int = 512

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        """Every engine runs on LASTCALL_RUNNER_IMAGE, unless LASTCALL_RUNNER_IMAGE_<ENGINE> sets another (for
        example LASTCALL_RUNNER_IMAGE_CLAUDE_CODE)."""
        image = env.get("LASTCALL_RUNNER_IMAGE", "lastcall-runner:dev")
        return cls(
            socket=Path(env.get("LASTCALL_SOCKET", "/run/lastcall/supervisor.sock")),
            data_root=Path(env.get("LASTCALL_DATA_ROOT", "/data")),
            data_volume=env.get("LASTCALL_DATA_VOLUME", "lastcall-data"),
            network=env.get("LASTCALL_SANDBOX_NETWORK", "lastcall-sandbox"),
            proxy=env.get("LASTCALL_EGRESS_PROXY", "http://egress:8888"),
            images={
                e: env.get(f"LASTCALL_RUNNER_IMAGE_{e.upper().replace('-', '_')}", image) for e in engines.BUILTIN
            },
            client_gid=int(env.get("LASTCALL_CLIENT_GID", "1000")),
        )


class Invalid(ValueError):
    """The request breaks a rule. Nothing runs."""


@dataclass(frozen=True)
class StepRequest:
    engine: str
    argv: tuple[str, ...]
    stdin: str
    env: Mapping[str, str]
    files: Mapping[str, str]  # /ctx/... -> content
    copies: Mapping[str, str]  # /ctx/... -> file under runs/, relative to the data root
    workspace: str  # directory under runs/, relative to the data root
    writable: bool
    account_volume: str | None
    home: str | None
    timeout: float
    image: str | None = None  # a project's environment, instead of the engine's runner image

    @classmethod
    def parse(cls, body: Mapping[str, Any], config: Config) -> StepRequest:
        engine = body.get("engine")
        if not isinstance(engine, str) or engine not in config.images:
            raise Invalid(f"unknown engine: {engine!r}")
        argv = body.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise Invalid("argv must be a non-empty list of strings")
        stdin = body.get("stdin", "")
        if not isinstance(stdin, str):
            raise Invalid("stdin must be a string")
        env = _strings(body.get("env"), "env")
        for name in env:
            if not ENV_NAME.fullmatch(name) or name in RESERVED_ENV:
                raise Invalid(f"environment variable not allowed: {name}")
        files = _strings(body.get("files"), "files")
        copies = _strings(body.get("copies"), "copies")
        for dest in [*files, *copies]:
            _ctx_path(dest)
        for src in copies.values():
            _data_path(config, src, directory=False)
        workspace = body.get("workspace")
        if not isinstance(workspace, str):
            raise Invalid("workspace must be a string")
        _data_path(config, workspace, directory=True)
        writable = body.get("writable", False)
        if not isinstance(writable, bool):
            raise Invalid("writable must be true or false")
        volume, home = body.get("account_volume"), body.get("home")
        if (volume is None) != (home is None):
            raise Invalid("account_volume and home go together")
        if volume is not None:
            if not isinstance(volume, str) or not ACCOUNT_VOLUME.fullmatch(volume):
                raise Invalid(f"not an account volume: {volume!r}")
            if not isinstance(home, str) or not _under(home, "/home/runner"):
                raise Invalid(f"home must be under /home/runner: {home!r}")
        timeout = body.get("timeout", 45 * 60.0)
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not 0 < timeout <= MAX_TIMEOUT:
            raise Invalid(f"timeout must be between 0 and {MAX_TIMEOUT:g} s")
        image = body.get("image")
        if image is not None:
            if not isinstance(image, str) or len(image) > 255 or not IMAGE.fullmatch(image):
                raise Invalid(f"not an image reference: {image!r}")
            if volume is not None:
                raise Invalid("an environment image never gets an account volume")
        return cls(
            engine, tuple(argv), stdin, env, files, copies, workspace, writable, volume, home, float(timeout), image
        )


def _strings(value: object, what: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise Invalid(f"{what} must map strings to strings")
    return dict(value)


def _under(path: str, root: str) -> bool:
    p = PurePosixPath(path)
    return (
        p.is_absolute() and ".." not in p.parts and SAFE_PATH.fullmatch(path) is not None and p.is_relative_to(root)
    )


def _ctx_path(dest: str) -> PurePosixPath:
    p = PurePosixPath(dest)
    if not _under(dest, "/ctx") or len(p.parts) < 3:
        raise Invalid(f"step files go under /ctx: {dest!r}")
    return p


def _data_path(config: Config, rel: str, directory: bool) -> Path:
    p = PurePosixPath(rel)
    if not SAFE_PATH.fullmatch(rel) or p.is_absolute() or ".." in p.parts or len(p.parts) < 2 or p.parts[0] != "runs":
        raise Invalid(f"path must be inside runs/ in the data volume: {rel!r}")
    full = (config.data_root / p).resolve()
    if not full.is_relative_to((config.data_root / "runs").resolve()):
        raise Invalid(f"path leaves runs/: {rel!r}")
    if not (full.is_dir() if directory else full.is_file()):
        raise Invalid(f"no such {'directory' if directory else 'file'}: {rel!r}")
    return full


def docker_argv(req: StepRequest, step_id: str, config: Config) -> list[str]:
    """The `docker run` command for one step. Isolation comes from here, never from the request."""
    argv = ["docker", "run", "--rm", "-i", "--name", container_name(step_id), "--network", config.network]
    argv += ["--user", f"{RUNNER_UID}:{RUNNER_UID}", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    argv += ["--memory", config.memory, "--cpus", config.cpus, "--pids-limit", str(config.pids)]
    env = {"HTTPS_PROXY": config.proxy, "HTTP_PROXY": config.proxy, "NO_PROXY": NO_PROXY, **req.env}
    for name, value in env.items():
        argv += ["-e", f"{name}={value}"]
    argv += ["--mount", _volume(config.data_volume, "/work", req.workspace, readonly=not req.writable)]
    argv += ["--mount", _volume(config.data_volume, "/ctx", f"steps/{step_id}/ctx", readonly=True)]
    if req.account_volume and req.home:
        argv += ["--mount", f"type=volume,src={req.account_volume},dst={req.home}"]
    return [*argv, "-w", "/work", req.image or config.images[req.engine], *req.argv]


def container_name(step_id: str) -> str:
    return f"lastcall-step-{step_id}"


def _volume(src: str, dst: str, subpath: str, readonly: bool) -> str:
    spec = f"type=volume,src={src},dst={dst},volume-subpath={subpath}"
    return f"{spec},readonly" if readonly else spec


def run_step(req: StepRequest, config: Config, send: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    """Prepare /ctx, run the container and stream its output. Stops the container if the client goes away."""
    step_id = uuid.uuid4().hex[:12]
    step_dir = config.data_root / "steps" / step_id
    ctx = step_dir / "ctx"
    ctx.mkdir(parents=True)
    try:
        for dest, content in req.files.items():
            target = ctx / PurePosixPath(dest).relative_to("/ctx")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for dest, src in req.copies.items():
            target = ctx / PurePosixPath(dest).relative_to("/ctx")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(config.data_root / src, target)

        def forward(line: str) -> bool:
            return send({"out": line.rstrip("\n")})

        def stop() -> None:
            subprocess.run(["docker", "rm", "-f", container_name(step_id)], capture_output=True, check=False)

        outcome = stream(docker_argv(req, step_id, config), req.stdin, req.timeout, forward, stop=stop)
        return {"exit": outcome.exit_code, "timed_out": outcome.timed_out, "stderr": outcome.stderr_tail}
    finally:
        shutil.rmtree(step_dir, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    config: Config  # set by make_server

    def do_POST(self) -> None:
        if self.path != "/steps":
            self._reply(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            self._reply(413, {"error": f"body must be between 1 and {MAX_BODY} bytes"})
            return
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise Invalid("the body must be a JSON object")
            req = StepRequest.parse(body, self.config)
        except ValueError as e:  # Invalid, or JSON that does not parse
            self._reply(400, {"error": str(e)})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()

        def send(frame: dict[str, Any]) -> bool:
            try:
                self.wfile.write((json.dumps(frame) + "\n").encode())
                self.wfile.flush()
                return True
            except OSError:
                return False

        t0 = time.monotonic()
        final = run_step(req, self.config, send)
        send(final)
        log.info(
            "step engine=%s image=%s workspace=%s writable=%s exit=%s timed_out=%s seconds=%.0f",
            req.engine,
            req.image or "runner",
            req.workspace,
            req.writable,
            final["exit"],
            final["timed_out"],
            time.monotonic() - t0,
        )

    def _reply(self, status: int, obj: dict[str, Any]) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        log.debug(format, *args)


def make_server(config: Config) -> socketserver.ThreadingUnixStreamServer:
    class ConfiguredHandler(Handler):
        pass

    ConfiguredHandler.config = config
    server = socketserver.ThreadingUnixStreamServer(str(config.socket), ConfiguredHandler)
    server.daemon_threads = True
    return server


def prepare(config: Config) -> None:
    """Start clean: no leftover step containers or /ctx folders, and runs/ owned by the service and runners."""
    leftovers = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "name=^lastcall-step-"], capture_output=True, text=True, check=False
    ).stdout.split()
    if leftovers:
        subprocess.run(["docker", "rm", "-f", *leftovers], capture_output=True, check=False)
    shutil.rmtree(config.data_root / "steps", ignore_errors=True)
    runs = config.data_root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    os.chown(runs, RUNNER_UID, RUNNER_UID)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = Config.from_env(os.environ)
    prepare(config)
    config.socket.parent.mkdir(parents=True, exist_ok=True)
    config.socket.unlink(missing_ok=True)
    server = make_server(config)
    os.chmod(config.socket, 0o660)
    os.chown(config.socket, -1, config.client_gid)
    log.info("listening on %s", config.socket)
    server.serve_forever()


if __name__ == "__main__":
    main()
