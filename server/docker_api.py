"""Minimal Docker Engine API client over the unix socket: only what the hub needs (restart a container)."""
from __future__ import annotations

import http.client
import json
import socket


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path: str, timeout: float = 10.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.sock_path = sock_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.sock_path)


def restart_container(name: str, sock_path: str = "/var/run/docker.sock", timeout_s: int = 20,
                      connection: http.client.HTTPConnection | None = None) -> None:
    """POST /containers/{name}/restart. Raises OSError/RuntimeError when the socket or container is missing."""
    conn = connection or UnixHTTPConnection(sock_path, timeout=timeout_s + 10)
    try:
        conn.request("POST", f"/containers/{name}/restart?t={timeout_s}")
        resp = conn.getresponse()
        body = resp.read().decode(errors="replace")
        if resp.status >= 300:
            raise RuntimeError(f"docker restart {name}: HTTP {resp.status} {body.strip()}")
    finally:
        conn.close()


def exec_container(name: str, cmd: list[str], sock_path: str = "/var/run/docker.sock",
                   timeout_s: int = 300, connection: http.client.HTTPConnection | None = None) -> str:
    """Run a command inside a running container over the Docker socket and return stdout."""
    conn = connection or UnixHTTPConnection(sock_path, timeout=timeout_s)
    hdrs = {"Content-Type": "application/json"}
    try:
        conn.request("POST", f"/containers/{name}/exec",
                     body=json.dumps({"AttachStdout": True, "AttachStderr": True, "Tty": True, "Cmd": cmd}),
                     headers=hdrs)
        r1 = conn.getresponse()
        b1 = r1.read().decode(errors="replace")
        if r1.status >= 300:
            raise RuntimeError(f"docker exec create {name}: HTTP {r1.status} {b1.strip()}")
        exec_id = json.loads(b1)["Id"]
        conn.request("POST", f"/exec/{exec_id}/start",
                     body=json.dumps({"Detach": False, "Tty": True}), headers=hdrs)
        r2 = conn.getresponse()
        b2 = r2.read().decode(errors="replace")
        if r2.status >= 300:
            raise RuntimeError(f"docker exec start {name}: HTTP {r2.status} {b2.strip()}")
        return b2
    finally:
        conn.close()
