"""Minimal Docker Engine API client over the unix socket: only what the hub needs (restart a container)."""
from __future__ import annotations

import http.client
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
