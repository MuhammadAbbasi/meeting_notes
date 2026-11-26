"""WebSocket link to the caption hub with exponential-backoff reconnect (CONTRACTS.md §1.1)."""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Iterator
from urllib.parse import quote

from websockets.sync.client import connect


REPLACED = 4001  # hub close code: this speaker name was taken over by a newer connection
LOOPBACK_ACTIVE = 4002  # hub close code: another PC is already captioning Remote-Call in this room


def friendly_state(state: str, meeting: str, paused: bool = False) -> str:
    """What the caption strip says about the connection, in words a person understands."""
    if state == "connected":
        return f"Paused · {meeting}" if paused else f"Live · {meeting}"
    if state.startswith("reconnecting"):
        return "Reconnecting to the hub…"
    if state == "stopped":
        return "Stopped. Start it again from the tray icon."
    return state[:1].upper() + state[1:]


def backoff_delays() -> Iterator[float]:
    d = 1
    while True:
        yield d
        d = min(d * 2, 30)


def speaker_url(server_ip: str, port: int, meeting_id: str, speaker: str, language: str) -> str:
    return f"ws://{server_ip}:{port}/ws/{meeting_id}/{quote(speaker)}?lang={language}"


def fetch_calendar(server_ip: str, port: int, timeout: float = 3.0) -> list[dict]:
    """GET /api/calendar; returns [] if the hub is unreachable."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{server_ip}:{port}/api/calendar", timeout=timeout) as r:
            return json.load(r).get("meetings", [])
    except Exception:
        return []


def finalize(server_ip: str, port: int, meeting_id: str, project_id: int | None = None,
             attendees: list[str] | None = None, language: str | None = None, timeout: float = 600) -> dict:
    """POST /finalize; blocking (Ollama can take minutes). Returns the hub's JSON or {"error": str}."""
    import urllib.error
    import urllib.request

    payload = {}
    if project_id is not None:
        payload["project_id"] = project_id
    if attendees is not None:
        payload["attendees"] = attendees
    if language:
        payload["language"] = language
    body = json.dumps(payload).encode("utf-8") if payload else b""
    headers = {"Content-Type": "application/json"} if payload else {}
    req = urllib.request.Request(f"http://{server_ip}:{port}/api/meetings/{meeting_id}/finalize",
                                 data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


class HubLink:
    def __init__(self, url: str, audio, on_message: Callable[[dict], None],
                 on_state: Callable[[str], None] | None = None) -> None:
        self.url, self.audio, self.on_message = url, audio, on_message
        self.on_state = on_state or (lambda s: None)
        self._stop = threading.Event()
        self._ws = None

    def run(self) -> None:
        delays = backoff_delays()
        while not self._stop.is_set():
            ws = None
            try:
                with connect(self.url, max_size=None) as ws:
                    self._ws = ws
                    self.on_state("connected")
                    delays = backoff_delays()
                    threading.Thread(target=self._pump, args=(ws,), daemon=True).start()
                    for raw in ws:
                        self.on_message(json.loads(raw))
            except Exception as e:  # refused, dropped, reset: all mean "retry"
                self.on_state(f"reconnecting ({type(e).__name__})")
            self._ws = None
            if ws is not None and ws.close_code == REPLACED:
                # another PC joined this meeting with the same name: retrying would kick it out in turn
                self.on_state("stopped: your name is in use on another PC (change it in config.json)")
                return
            if ws is not None and ws.close_code == LOOPBACK_ACTIVE:
                self.on_state("stopped: another PC is already captioning call audio")
                return
            if self._stop.is_set():
                break
            time.sleep(next(delays))
        self.on_state("stopped")

    def _pump(self, ws) -> None:
        while not self._stop.is_set() and self._ws is ws:
            data = self.audio.read(timeout=0.5)
            if data:
                try:
                    ws.send(data)
                except Exception:
                    return

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.send(json.dumps({"type": "end"}))
                ws.close()
            except Exception:
                pass
