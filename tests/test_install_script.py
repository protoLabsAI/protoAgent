"""scripts/install.sh against a token-protected instance (#4315).

The installer's HTTP helpers must carry the operator bearer on every setup
request (readiness probe, wizard, and the PROTOAGENT_INSTALL_URL path), tell a
401/403 apart from a server that never came up, and never print the token —
not even under ``sh -x``. Docker is stubbed; the "instance" is a tiny local HTTP
server that enforces the bearer like the real /api/config/* routes do.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "install.sh"
SH = shutil.which("sh")
TOKEN = "op-token-4315-d3adb33f"

pytestmark = [
    pytest.mark.skipif(os.name == "nt" or SH is None, reason="install.sh is POSIX sh"),
    pytest.mark.skipif(shutil.which("curl") is None, reason="install.sh drives curl"),
    pytest.mark.platform_sensitive,
]


class _Instance:
    """A fake protoAgent: /api/config/* behind an optional bearer token."""

    def __init__(self, token: str | None):
        self.token = token
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep pytest output clean
                pass

            def _reply(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode() if length else ""
                auth = self.headers.get("Authorization")
                outer.requests.append({"method": method, "path": self.path, "auth": auth, "body": body})
                if outer.token is not None and auth != f"Bearer {outer.token}":
                    self._reply(401, {"detail": "unauthorized"})
                    return
                routes = {
                    "/api/config/setup-status": {"setup_complete": False},
                    "/api/config/models": {"models": ["model-a", "model-b"]},
                    "/api/config/test-model": {"ok": True},
                    "/api/config/setup": {"ok": True},
                }
                if self.path in routes:
                    self._reply(200, routes[self.path])
                else:
                    self._reply(404, {"detail": "not found"})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _stub_docker(tmp_path: Path) -> Path:
    """A docker that succeeds and records its argv + the token env it was handed."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{tmp_path}/docker-args"\n'
        f'[ "$1" = run ] && printf "%s" "${{A2A_AUTH_TOKEN-<unset>}}" > "{tmp_path}/docker-env"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return bindir


def _env(
    tmp_path: Path, token: str | None, *, platform: str | None = "linux/amd64", **extra: str
) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PROTOAGENT_", "A2A_"))}
    env.update(NO_COLOR="1", TMPDIR=str(tmp_path), **extra)
    # Pin PLATFORM by default so the stub docker's argv is deterministic; pass
    # platform=None to instead exercise the script's `uname -m` auto-detection.
    if platform is not None:
        env["PROTOAGENT_PLATFORM"] = platform
    if token is not None:
        env["A2A_AUTH_TOKEN"] = token
    return env


def _stub_uname(bindir: Path, machine: str) -> None:
    """A `uname` that reports a chosen `-m`; any other flag defers to the real one."""
    real = shutil.which("uname") or "/usr/bin/uname"
    uname = bindir / "uname"
    uname.write_text(
        "#!/bin/sh\n"
        f'[ "$1" = -m ] && {{ printf "%s\\n" "{machine}"; exit 0; }}\n'
        f'exec "{real}" "$@"\n',
        encoding="utf-8",
    )
    uname.chmod(0o755)


def _run_docker_path(tmp_path: Path, instance: _Instance, token: str | None) -> subprocess.CompletedProcess:
    bindir = _stub_docker(tmp_path)
    env = _env(tmp_path, token, PROTOAGENT_PORT=str(instance.port), PROTOAGENT_INSTALL_NONINTERACTIVE="1")
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    # `-x`: the trace must not leak the token either.
    return subprocess.run(
        [SH, "-x", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL
    )


def test_readiness_probe_sends_bearer_and_never_prints_it(tmp_path):
    with _Instance(TOKEN) as inst:
        res = _run_docker_path(tmp_path, inst, TOKEN)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Server is up." in res.stdout
    assert inst.requests and all(r["auth"] == f"Bearer {TOKEN}" for r in inst.requests)
    assert TOKEN not in res.stdout and TOKEN not in res.stderr
    # The container still gets the token — through the environment, not argv.
    assert (tmp_path / "docker-env").read_text() == TOKEN
    assert TOKEN not in (tmp_path / "docker-args").read_text()
    assert "-e A2A_AUTH_TOKEN " in (tmp_path / "docker-args").read_text()
    # The private header file is cleaned up on exit.
    assert not list(tmp_path.glob("protoagent-auth.*"))


def test_open_loopback_install_needs_no_token(tmp_path):
    with _Instance(None) as inst:
        res = _run_docker_path(tmp_path, inst, None)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Server is up." in res.stdout
    assert inst.requests and all(r["auth"] is None for r in inst.requests)


@pytest.mark.parametrize(
    ("machine", "forces_amd64"),
    [
        ("x86_64", False),   # native amd64
        ("aarch64", False),  # native arm64 — the multi-arch image resolves it
        ("arm64", False),    # Apple Silicon reports arm64; also native now
        ("armv7l", True),    # no native variant → amd64 under emulation
    ],
)
def test_arch_detection_pulls_native_on_amd64_and_arm64(tmp_path, machine, forces_amd64):
    """After multi-arch publishing, arm64 hosts must pull the native image (no
    `--platform`), not amd64 under emulation (the #4571 installer regression)."""
    bindir = _stub_docker(tmp_path)
    _stub_uname(bindir, machine)
    with _Instance(None) as inst:
        env = _env(
            tmp_path,
            None,
            platform=None,  # exercise the script's own `uname -m` detection
            PROTOAGENT_PORT=str(inst.port),
            PROTOAGENT_INSTALL_NONINTERACTIVE="1",
        )
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        res = subprocess.run(
            [SH, str(SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    assert res.returncode == 0, res.stdout + res.stderr
    args = (tmp_path / "docker-args").read_text()
    if forces_amd64:
        assert "--platform linux/amd64" in args
    else:
        assert "--platform" not in args


def test_missing_token_is_an_auth_failure_not_a_startup_timeout(tmp_path):
    with _Instance(TOKEN) as inst:
        started = time.monotonic()
        res = _run_docker_path(tmp_path, inst, None)
    assert res.returncode != 0
    assert "requires an operator token (HTTP 401)" in res.stderr
    assert "did not come up in time" not in res.stderr
    assert time.monotonic() - started < 30  # fails fast instead of polling 90x


def test_wrong_token_is_reported_as_rejected_without_echoing_it(tmp_path):
    with _Instance(TOKEN) as inst:
        res = _run_docker_path(tmp_path, inst, "not-the-token-77")
    assert res.returncode != 0
    assert "rejected the operator token (HTTP 401)" in res.stderr
    assert "did not come up in time" not in res.stderr
    assert "not-the-token-77" not in res.stdout + res.stderr


def test_install_url_noninteractive_without_token_reports_auth(tmp_path):
    with _Instance(TOKEN) as inst:
        res = subprocess.run(
            [SH, str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            env=_env(tmp_path, None, PROTOAGENT_INSTALL_URL=inst.url, PROTOAGENT_INSTALL_NONINTERACTIVE="1"),
        )
    assert res.returncode != 0
    assert "requires an operator token (HTTP 401)" in res.stderr
    assert "Cannot reach" not in res.stderr


def _expect(master: int, transcript: bytearray, needle: bytes, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while needle not in transcript:
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError(f"timed out waiting for {needle!r}; got:\n{transcript.decode(errors='replace')}")
        ready, _, _ = select.select([master], [], [], left)
        if ready:
            try:
                chunk = os.read(master, 4096)
            except OSError:  # EIO: the child closed the pty
                chunk = b""
            if not chunk:
                raise AssertionError(f"EOF waiting for {needle!r}; got:\n{transcript.decode(errors='replace')}")
            transcript.extend(chunk)


def _drain(master: int, transcript: bytearray, timeout: float = 30) -> None:
    """Read the pty until the child closes it (EOF/EIO)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.5)
        if not ready:
            continue
        try:
            chunk = os.read(master, 4096)
        except OSError:
            return
        if not chunk:
            return
        transcript.extend(chunk)


def test_install_url_wizard_sends_bearer_on_every_request(tmp_path):
    import fcntl
    import pty
    import termios

    master, slave = pty.openpty()

    def _ctty():  # the wizard prompts on /dev/tty: give the child one
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    with _Instance(TOKEN) as inst:
        proc = subprocess.Popen(
            [SH, "-x", str(SCRIPT)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=_env(tmp_path, TOKEN, PROTOAGENT_INSTALL_URL=inst.url),
            start_new_session=True,
            preexec_fn=_ctty,
        )
        os.close(slave)
        out = bytearray()
        try:
            for prompt, answer in (
                (b"Agent name", b"\n"),
                (b"Model gateway URL", b"http://gw.example/v1\n"),
                (b"API key", b"sk-gateway-key\n"),
                (b"Choose a number", b"2\n"),
                (b"protoAgent is running.", None),
            ):
                _expect(master, out, prompt)
                if answer is not None:
                    os.write(master, answer)
            _drain(master, out)  # macOS: a pty with unread output blocks the child's exit
            assert proc.wait(timeout=30) == 0, out.decode(errors="replace")
        finally:
            if proc.poll() is None:
                proc.kill()
                _drain(master, out)
                proc.wait(timeout=30)
            os.close(master)

    paths = [r["path"] for r in inst.requests]
    assert paths == [
        "/api/config/setup-status",
        "/api/config/models",
        "/api/config/test-model",
        "/api/config/setup",
    ]
    assert all(r["auth"] == f"Bearer {TOKEN}" for r in inst.requests)
    setup = json.loads(inst.requests[-1]["body"])["config"]
    assert setup["model"]["name"] == "model-b"
    assert TOKEN.encode() not in out
    assert b"Setup complete." in out
