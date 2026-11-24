"""Office Meeting Agent: mic (+ optional system audio) -> caption hub, live overlay, tray control.
Run: python client/office_agent.py   (or OfficeMeetingAgent.exe built by build_exe.bat)"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
from pathlib import Path

from audio_capture import LoopbackCapture, MicCapture
from hub_link import HubLink, finalize, speaker_url
from overlay_ui import CaptionOverlay
from setup_dialog import ask_config, ask_meeting
from tray import Tray

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))  # bundled resources when frozen
CONFIG = Path(sys.executable).parent / "config.json" if getattr(sys, "frozen", False) \
    else Path(__file__).with_name("config.json")
MEETING_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")  # CONTRACTS §1.1


def save_config(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def load_config() -> dict:
    """config.json next to the exe; a pre-filled one from the hub (/download/config.json) has no user_name yet."""
    defaults = json.loads((HERE / "config.example.json").read_text(encoding="utf-8"))
    if CONFIG.exists():
        defaults.update(json.loads(CONFIG.read_text(encoding="utf-8")))
        if defaults.get("user_name"):
            return defaults
    cfg = ask_config(defaults)
    if cfg is None:
        sys.exit(0)
    save_config(cfg)
    return cfg


class Agent:
    """Owns audio sources, hub links, overlay and tray; start/stop/pause state. Tray callbacks are marshalled to Tk."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.audio = MicCapture(device=cfg.get("input_device"))
        self.loop_audio = LoopbackCapture() if cfg.get("capture_system_audio") else None
        self.overlay = CaptionOverlay(on_pause=self.set_paused, on_finalize=self.finalize,
                                      alpha=cfg.get("overlay_alpha", 0.5))
        self.tray = Tray(self)
        self.link: HubLink | None = None
        self.loop_link: HubLink | None = None
        self.paused = False

    @property
    def running(self) -> bool:
        return self.link is not None

    def _url(self, speaker: str) -> str:
        return speaker_url(self.cfg["server_ip"], self.cfg.get("hub_port", 8085), self.cfg["meeting_id"],
                           speaker, self.cfg.get("language", "auto"))

    def _on_state(self, state: str) -> None:
        self.overlay.set_status(state)
        self.tray.set_state("paused" if state == "connected" and self.paused else state)

    def start(self) -> None:
        if self.running:
            return
        self.audio.start()
        self.link = HubLink(self._url(self.cfg["user_name"]), self.audio, self.overlay.push, self._on_state)
        threading.Thread(target=self.link.run, daemon=True).start()
        if self.loop_audio is not None and self.loop_audio.start():
            self.loop_link = HubLink(self._url("Remote-Call"), self.loop_audio, lambda m: None)
            threading.Thread(target=self.loop_link.run, daemon=True).start()

    def stop(self) -> None:
        if not self.running:
            return
        for link in (self.link, self.loop_link):
            if link is not None:
                link.stop()
        self.link = self.loop_link = None
        self.audio.stop()
        if self.loop_audio is not None:
            self.loop_audio.stop()
        self._on_state("stopped")

    def toggle_running(self) -> None:
        self.stop() if self.running else self.start()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.audio.muted = paused
        if self.loop_audio is not None:
            self.loop_audio.muted = paused
        self.overlay.call(lambda: self.overlay.set_paused(paused))
        if self.running:
            self.tray.set_state("paused" if paused else "connected")

    def finalize(self) -> None:
        """Ask the hub for minutes + Odoo push; runs in a thread, result shown in the status line."""
        def work():
            self.overlay.set_status("finalizing… (LLM may take a few minutes)")
            r = finalize(self.cfg["server_ip"], self.cfg.get("hub_port", 8085), self.cfg["meeting_id"])
            if "error" in r:
                self.overlay.set_status(f"finalize failed: {r['error']}")
            else:
                self.overlay.set_status(f"minutes saved ({'LLM' if r['llm'] else 'transcript'}, odoo: {r['odoo']['status']})")
        threading.Thread(target=work, daemon=True).start()

    def change_meeting(self) -> None:
        def on_tk():
            new = ask_meeting(self.overlay.root, self.cfg["meeting_id"])
            if new and MEETING_ID_RE.fullmatch(new):
                self.stop()
                self.cfg["meeting_id"] = new
                save_config(self.cfg)
                self.start()
        self.overlay.call(on_tk)

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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Agent(load_config()).run()
