"""System tray icon (pystray). Runs detached; menu callbacks arrive on the tray thread."""
from __future__ import annotations

import pystray
from PIL import Image, ImageDraw

COLORS = {"connected": "#2e7d32", "paused": "#f9a825", "stopped": "#616161", "reconnecting": "#c62828"}


def _icon(color: str) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 10, 60, 50), radius=12, fill=color)
    d.polygon([(18, 48), (30, 48), (18, 60)], fill=color)  # speech-bubble tail
    d.text((16, 18), "cc", fill="white")
    return img


class Tray:
    def __init__(self, agent) -> None:
        self.agent = agent
        a = agent
        menu = pystray.Menu(
            pystray.MenuItem(lambda i: "Hide captions" if a.overlay.visible else "Show captions",
                             lambda: a.toggle_overlay(), default=True),
            pystray.MenuItem(lambda i: "Stop streaming" if a.running else "Start streaming",
                             lambda: a.toggle_running()),
            pystray.MenuItem(lambda i: "Resume" if a.paused else "Pause (send silence)",
                             lambda: a.set_paused(not a.paused), enabled=lambda i: a.running),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Finalize & push to Odoo", lambda: a.finalize()),
            pystray.MenuItem("Change meeting…", lambda: a.change_meeting()),
            pystray.MenuItem(lambda i: f"Meeting: {a.cfg['meeting_id']} · {a.cfg['user_name']}", None, enabled=False),
            pystray.MenuItem("Quit", lambda: a.quit()),
        )
        self.icon = pystray.Icon("OfficeMeetingAgent", _icon(COLORS["stopped"]), "Office captions", menu)

    def start(self) -> None:
        self.icon.run_detached()

    def set_state(self, state: str) -> None:
        key = "reconnecting" if state.startswith("reconnecting") else state
        self.icon.icon = _icon(COLORS.get(key, COLORS["stopped"]))
        self.icon.title = f"Office captions · {state}"

    def stop(self) -> None:
        self.icon.stop()
