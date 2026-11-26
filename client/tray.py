"""System tray icon (pystray). Runs detached; menu callbacks arrive on the tray thread."""
from __future__ import annotations

import pystray
from PIL import Image, ImageDraw

COLORS = {"connected": "#1E8E3E", "paused": "#F9AB00", "stopped": "#80868B", "reconnecting": "#D93025"}


def _icon(color: str) -> Image.Image:
    """The app logo with a status dot (green streaming, yellow paused, grey stopped, red reconnecting)."""
    from setup_dialog import ASSETS
    try:
        img = Image.open(ASSETS / "icon.png").convert("RGBA").resize((64, 64), Image.LANCZOS)
    except OSError:
        img = Image.new("RGBA", (64, 64), "#0B57D0")
    d = ImageDraw.Draw(img)
    d.ellipse((38, 38, 63, 63), fill="white")
    d.ellipse((42, 42, 59, 59), fill=color)
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
            pystray.MenuItem("End meeting & write notes…", lambda: a.prompt_end_meeting("manual"),
                             enabled=lambda i: a.running),
            pystray.MenuItem("Change meeting…", lambda: a.change_meeting()),
            pystray.MenuItem("Open live captions", lambda: a.open_url("captions")),
            pystray.MenuItem("Open dashboard", lambda: a.open_url("dashboard")),
            pystray.MenuItem(lambda i: f"Meeting: {a.cfg['meeting_id']} · {a.cfg['user_name']}", None, enabled=False),
            pystray.MenuItem("Quit", lambda: a.prompt_quit()),
        )
        self.icon = pystray.Icon("OfficeMeetingAgent", _icon(COLORS["stopped"]), "Meeting Captions", menu)

    def start(self) -> None:
        self.icon.run_detached()

    def set_state(self, state: str) -> None:
        key = "reconnecting" if state.startswith("reconnecting") else state
        self.icon.icon = _icon(COLORS.get(key, COLORS["stopped"]))
        self.icon.title = f"Meeting Captions: {state}"

    def stop(self) -> None:
        self.icon.stop()
