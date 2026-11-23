"""WebSocket link to the caption hub with exponential-backoff reconnect (CONTRACTS.md §1.1)."""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Iterator
from urllib.parse import quote

from websockets.sync.client import connect


def backoff_delays() -> Iterator[float]:
    d = 1
    while True:
        yield d
        d = min(d * 2, 30)


def speaker_url(server_ip: str, port: int, meeting_id: str, speaker: str, language: str) -> str:
    return f"ws://{server_ip}:{port}/ws/{meeting_id}/{quote(speaker)}?lang={language}"


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
