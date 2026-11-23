"""Office Meeting Agent: mic -> caption hub, live overlay. Run: python client/office_agent.py"""
from __future__ import annotations

import json
import shutil
import sys
import threading
from pathlib import Path

from audio_capture import MicCapture
from hub_link import HubLink, speaker_url
from overlay_ui import CaptionOverlay

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))  # PyInstaller-friendly (M4)
CONFIG = Path(sys.executable).parent / "config.json" if getattr(sys, "frozen", False) \
    else Path(__file__).with_name("config.json")


def load_config() -> dict:
    if not CONFIG.exists():
        shutil.copy(HERE / "config.example.json", CONFIG)
        print(f"Created {CONFIG}. Edit user_name / server_ip / meeting_id, then start again.")
        sys.exit(1)
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def main() -> None:
    cfg = load_config()
    audio = MicCapture(device=cfg.get("input_device"))
    audio.start()
    overlay = CaptionOverlay(on_mute=lambda m: setattr(audio, "muted", m))
    url = speaker_url(cfg["server_ip"], cfg.get("hub_port", 8085), cfg["meeting_id"],
                      cfg["user_name"], cfg.get("language", "auto"))
    link = HubLink(url, audio, overlay.push, overlay.set_status)
    threading.Thread(target=link.run, daemon=True).start()
    try:
        overlay.run()
    finally:
        link.stop()
        audio.stop()


if __name__ == "__main__":
    main()
