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
    """What the caption strip says about the connection, in Italian plain language."""
    if state == "connected":
        return f"In pausa · {meeting}" if paused else f"In diretta · {meeting}"
    if state.startswith("reconnecting"):
        return "Riconnessione al server…"
    if state == "stopped":
        return "Fermo. Avvia una riunione dall'icona vicino all'orologio."
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


def match_odoo_user(server_ip: str, port: int, name: str, email: str, timeout: float = 3.0) -> tuple[bool, str, str]:
    """Match (name, email) against Odoo users on the hub.
    Returns (ok, canonical_name, message). When the hub/Odoo is unreachable or unconfigured, returns (True, name, '')."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{server_ip}:{port}/api/odoo/users", timeout=timeout) as r:
            users = json.load(r).get("users", [])
    except Exception:
        return True, name, ""
    if not users:
        return True, name, ""
    em = email.strip().lower()
    for u in users:
        if (u.get("email") or "").strip().lower() == em:
            return True, u["name"], ""
    nm = name.strip().lower()
    for u in users:
        if (u.get("name") or "").strip().lower() == nm:
            return True, u["name"], ""
    return False, name, "Email non trovata su Odoo. Controlla la tua email aziendale."


def finalize(server_ip: str, port: int, meeting_id: str, project_id: int | None = None,
             attendees: list[str] | None = None, language: str | None = None,
             publish: bool = True, timeout: float = 600) -> dict:
    """POST /finalize; blocking (Ollama can take minutes). Returns the hub's JSON or {"error": str}."""
    import urllib.error
    import urllib.request

    payload: dict = {"publish": publish}
    if project_id is not None:
        payload["project_id"] = project_id
    if attendees is not None:
        payload["attendees"] = attendees
    if language:
        payload["language"] = language
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(f"http://{server_ip}:{port}/api/meetings/{meeting_id}/finalize",
                                 data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def publish_draft(server_ip: str, port: int, meeting_id: str, markdown: str,
                  segments: list[dict] | None = None, timeout: float = 120) -> dict:
    """POST /publish with user-reviewed markdown/segments."""
    import urllib.error
    import urllib.request

    payload: dict = {"markdown": markdown}
    if segments is not None:
        payload["segments"] = segments
    req = urllib.request.Request(f"http://{server_ip}:{port}/api/meetings/{meeting_id}/publish",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
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
