"""pytest 公共夹具：用真实 uvicorn 子进程跑后端，SQLite 文件放在临时目录。

每个用例拿到的是独立的数据库文件；“重启”通过终止子进程后用同一文件
再次启动新进程来模拟，与容器重启后重新挂载同一数据卷等价。
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ApiServer:
    def __init__(self, db_path: Path, port: int):
        self.db_path = db_path
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self.proc: subprocess.Popen | None = None

    def start(self) -> "ApiServer":
        env = os.environ.copy()
        env["TOKEN_DATABASE_URL"] = f"sqlite:///{self.db_path}"
        env["PYTHONPATH"] = str(BACKEND_DIR)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.asgi:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=BACKEND_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.proc.poll() is not None:
                output = self.proc.stdout.read().decode() if self.proc.stdout else ""
                raise RuntimeError(f"uvicorn exited early:\n{output}")
            try:
                resp = httpx.get(f"{self.base_url}/health", timeout=0.5)
                if resp.status_code == 200:
                    return self
            except httpx.TransportError:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError("server did not become ready in time")

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.proc = None

    def restart(self) -> "ApiServer":
        """同一数据库文件上重启，模拟容器重启后挂回同一数据卷。

        原地重启（只换进程和端口），保证外层 fixture 结束时能清理到新进程。
        """
        self.stop()
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        return self.start()


@pytest.fixture
def api_server(tmp_path: Path):
    db_path = tmp_path / "token.db"
    server = ApiServer(db_path, _free_port()).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def fresh_db_path(tmp_path: Path) -> Path:
    return tmp_path / "fresh_token.db"
