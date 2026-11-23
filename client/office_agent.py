"""Office Meeting Agent: mic -> caption hub, live overlay, tray control. Run: python client/office_agent.py"""
from __future__ import annotations

import json
import shutil
import sys
import threading
from pathlib import Path

from audio_capture import MicCapture
from hub_link import HubLink, speaker_url
from overlay_ui import CaptionOverlay
from tray import Tray

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))  # PyInstaller-friendly (M4)
CONFIG = Path(sys.executable).parent / "config.json" if getattr(sys, "frozen", False) \
    else Path(__file__).with_name("config.json")


def load_config() -> dict:
    if not CONFIG.exists():
        shutil.copy(HERE / "config.example.json", CONFIG)
        print(f"Created {CONFIG}. Edit user_name / server_ip / meeting_id, then start again.")
        sys.exit(1)
    return json.loads(CONFIG.read_text(encoding="utf-8"))


class Agent:
    """Owns the three parts and the start/stop/pause state. Tray callbacks are marshalled to Tk."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.audio = MicCapture(device=cfg.get("input_device"))
        self.overlay = CaptionOverlay(on_pause=self.set_paused, alpha=cfg.get("overlay_alpha", 0.5))
        self.tray = Tray(self)
        self.link: HubLink | None = None
        self.paused = False

    @property
    def running(self) -> bool:
        return self.link is not None

    def _on_state(self, state: str) -> None:
        self.overlay.set_status(state)
        self.tray.set_state("paused" if state == "connected" and self.paused else state)

    def start(self) -> None:
        if self.running:
            return
        self.audio.start()
        url = speaker_url(self.cfg["server_ip"], self.cfg.get("hub_port", 8085), self.cfg["meeting_id"],
                          self.cfg["user_name"], self.cfg.get("language", "auto"))
        self.link = HubLink(url, self.audio, self.overlay.push, self._on_state)
        threading.Thread(target=self.link.run, daemon=True).start()

    def stop(self) -> None:
        if not self.running:
            return
        self.link.stop()
        self.link = None
        self.audio.stop()
        self._on_state("stopped")

    def toggle_running(self) -> None:
        self.stop() if self.running else self.start()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.audio.muted = paused
        self.overlay.call(lambda: self.overlay.set_paused(paused))
        if self.running:
            self.tray.set_state("paused" if paused else "connected")

    def toggle_overlay(self) -> None:
        self.overlay.call(self.overlay.hide if self.overlay.visible else self.overlay.show)

    def quit(self) -> None:
        self.stop()
        self.tray.stop()
        self.overlay.call(self.overlay.root.destroy)

    def run(self) -> None:
        self.start()
        threading.Thread(target=self.tray.start, daemon=True).start()  # pystray setup takes ~4 s on Windows
        try:
            self.overlay.run()
        finally:
            self.stop()
            self.tray.stop()


if __name__ == "__main__":
    Agent(load_config()).run()
