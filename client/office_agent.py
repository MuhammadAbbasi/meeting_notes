"""Office Meeting Agent: mic (+ optional system audio) -> caption hub, live overlay, tray control.
Run: python client/office_agent.py   (or OfficeMeetingAgent.exe built by build_exe.bat)"""
from __future__ import annotations

import json
import logging
import re
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from audio_capture import LoopbackCapture, MicCapture
from hub_link import HubLink, fetch_calendar, finalize, friendly_state, speaker_url
from overlay_ui import CaptionOverlay
from setup_dialog import ask_config, ask_end_meeting, ask_meeting
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
        self.overlay = CaptionOverlay(on_pause=self.set_paused, on_finalize=lambda: self.prompt_end_meeting("manual"),
                                      alpha=cfg.get("overlay_alpha", 0.5))
        self.tray = Tray(self)
        self.link: HubLink | None = None
        self.loop_link: HubLink | None = None
        self.paused = False
        self._meeting_meta: dict = {}
        self._last_activity = time.monotonic()
        self._had_others = False
        self._solo_since: float | None = None
        self._end_prompted = False
        self.overlay.root.after(30000, self._watch_end)

    @property
    def running(self) -> bool:
        return self.link is not None

    def _url(self, speaker: str) -> str:
        return speaker_url(self.cfg["server_ip"], self.cfg.get("hub_port", 8085), self.cfg["meeting_id"],
                           speaker, self.cfg.get("language", "auto"))

    def _on_state(self, state: str) -> None:
        self.overlay.set_status(friendly_state(state, self.cfg["meeting_id"], self.paused))
        self.tray.set_state("paused" if state == "connected" and self.paused else state)

    def _base_http(self) -> str:
        return f"http://{self.cfg['server_ip']}:{self.cfg.get('hub_port', 8085)}"

    def open_url(self, target: str) -> None:
        mid = self.cfg["meeting_id"]
        path = f"/captions/{mid}" if target == "captions" else "/dashboard"
        webbrowser.open(self._base_http() + path)

    def _refresh_calendar_meta(self) -> None:
        def work():
            for m in fetch_calendar(self.cfg["server_ip"], self.cfg.get("hub_port", 8085)):
                if m.get("meeting_id") == self.cfg["meeting_id"]:
                    self._meeting_meta = m
                    return
        threading.Thread(target=work, daemon=True).start()

    def _on_hub_message(self, frame: dict) -> None:
        self.overlay.push(frame)
        t = frame.get("type")
        if t == "caption":
            self._last_activity = time.monotonic()
        elif t == "roster":
            others = [s for s in frame.get("speakers", []) if s not in {self.cfg["user_name"], "Remote-Call"}]
            if others:
                self._had_others, self._solo_since = True, None
            elif self._had_others and self._solo_since is None:
                self._solo_since = time.monotonic()

    def _watch_end(self) -> None:
        if self.running and not self.paused and not self._end_prompted:
            now = time.monotonic()
            reason = None
            if self._solo_since is not None and now - self._solo_since >= 60:
                reason = "empty"
            elif now - self._last_activity >= 600:
                reason = "idle"
            elif self._meeting_meta.get("stop"):
                try:
                    stop_dt = datetime.strptime(self._meeting_meta["stop"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc).timestamp() - stop_dt.timestamp() >= 120:
                        reason = "scheduled"
                except ValueError:
                    pass
            if reason:
                self._end_prompted = True
                self.prompt_end_meeting(reason)
        self.overlay.root.after(30000, self._watch_end)

    def start(self) -> None:
        if self.running:
            return
        self._last_activity = time.monotonic()
        self._had_others, self._solo_since, self._end_prompted = False, None, False
        self._meeting_meta = {}
        self._refresh_calendar_meta()
        self.overlay.set_running(True)
        self.audio.start()
        self.link = HubLink(self._url(self.cfg["user_name"]), self.audio, self._on_hub_message, self._on_state)
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
        self.overlay.set_running(False)
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
            self._on_state("connected")  # strip and tray switch between "Live" and "Paused"
        if self.running:
            self.tray.set_state("paused" if paused else "connected")

    def finalize(self, quit_after: bool = False) -> None:
        """Stop streaming, flush audio, and ask the hub for minutes + Odoo push."""
        mid = self.cfg["meeting_id"]
        lang = self.cfg.get("language", "it")
        meta = dict(self._meeting_meta)
        self.stop()

        def work():
            self.overlay.set_status("Writing minutes… this takes one to three minutes")
            r = finalize(self.cfg["server_ip"], self.cfg.get("hub_port", 8085), mid,
                         project_id=meta.get("project_id"),
                         attendees=meta.get("attendees"),
                         language=lang)
            if "error" in r:
                self.overlay.set_status(f"Couldn't write the minutes: {r['error']}")
            else:
                try:
                    notes_dir = Path(self.cfg.get("notes_dir", "C:/a176lab_meetings"))
                    notes_dir.mkdir(parents=True, exist_ok=True)
                    (notes_dir / f"{mid}.minutes.md").write_text(r["markdown"], encoding="utf-8")
                except OSError:
                    pass
                where = "and published to Odoo" if r["odoo"]["status"] == "published" else "and saved on the hub"
                self.overlay.set_status(f"Minutes written {where}" if r["llm"]
                                        else "Only the transcript was saved: the minutes writer isn't available")
                if not quit_after:
                    self.overlay.show_notes_link(f"{self._base_http()}/dashboard#meeting/{mid}")
            if quit_after:
                self.quit()
        threading.Thread(target=work, daemon=True).start()

    def prompt_end_meeting(self, reason: str = "manual", quit_after: bool = False) -> None:
        def on_tk():
            choice = ask_end_meeting(self.overlay.root, self.cfg["meeting_id"], reason)
            if choice == "finalize":
                self.finalize(quit_after=quit_after)
            elif choice == "stop":
                self.stop()
                if quit_after:
                    self.quit()
        self.overlay.call(on_tk)

    def prompt_quit(self) -> None:
        if self.running:
            self.prompt_end_meeting("manual", quit_after=True)
        else:
            self.quit()

    def change_meeting(self) -> None:
        def fetch_and_ask():
            meetings = fetch_calendar(self.cfg["server_ip"], self.cfg.get("hub_port", 8085))

            def on_tk():
                picked = ask_meeting(self.overlay.root, self.cfg["meeting_id"], meetings,
                                     current_lang=self.cfg.get("language", "it"))
                if picked and MEETING_ID_RE.fullmatch(picked[0]):
                    new, lang = picked
                    if self.running:
                        choice = ask_end_meeting(self.overlay.root, self.cfg["meeting_id"], "manual")
                        if choice == "cancel":
                            return
                        if choice == "finalize":
                            self.finalize()
                        else:
                            self.stop()
                    self.cfg["meeting_id"] = new
                    self.cfg["language"] = lang
                    save_config(self.cfg)
                    self.start()
            self.overlay.call(on_tk)
        threading.Thread(target=fetch_and_ask, daemon=True).start()

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


def _acquire_single_instance() -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 48085))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _lock_sock = _acquire_single_instance()
    if _lock_sock is None:
        logging.info("OfficeMeetingAgent is already running.")
        sys.exit(0)
    Agent(load_config()).run()
