"""First-run setup window and meeting switcher (tkinter), in the same Google style as the dashboard."""
from __future__ import annotations

import re
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "assets"
SPEAKER_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,40}$")  # same rule as the hub (CONTRACTS 1.1)
MEETING_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LANGUAGES = [("Italian", "it"), ("English", "en"), ("German", "de"), ("French", "fr"), ("Spanish", "es"),
             ("Detect automatically", "auto")]
BLUE, INK, MUTED, LINE, ERROR, BG, HEAD = "#0B57D0", "#1F1F1F", "#444746", "#C4C7C5", "#B3261E", "#FFFFFF", "#F0F4F9"
FONT = "Segoe UI"


def set_window_icon(win: tk.Misc) -> None:
    try:
        win.iconbitmap(default=str(ASSETS / "icon.ico"))
    except tk.TclError:
        pass


def _label(parent, text, size=10, color=INK, bold=False, wrap=420, **pack):
    w = tk.Label(parent, text=text, bg=parent["bg"], fg=color, font=(FONT, size, "bold" if bold else "normal"),
                 justify="left", anchor="w", wraplength=wrap)
    w.pack(fill="x", **pack)
    return w


def _entry(parent, value):
    var = tk.StringVar(value=value)
    e = tk.Entry(parent, textvariable=var, font=(FONT, 11), relief="flat", bg=BG, fg=INK, insertbackground=INK,
                 highlightthickness=1, highlightbackground=LINE, highlightcolor=BLUE)
    e.pack(fill="x", ipady=6, pady=(4, 0))
    return var, e


def _button(parent, text, command, primary):
    return tk.Button(parent, text=text, command=command, cursor="hand2", relief="flat", bd=0, padx=20, pady=7,
                     font=(FONT, 10, "bold"), bg=BLUE if primary else BG, fg="white" if primary else BLUE,
                     activebackground="#0842A0" if primary else "#E8F0FE", activeforeground="white" if primary else BLUE)


def ask_config(defaults: dict) -> dict | None:
    """Setup window; returns the config dict, or None if the person cancels."""
    root = tk.Tk()
    root.title("Meeting Captions – set up")
    root.configure(bg=BG)
    root.resizable(False, False)
    set_window_icon(root)
    result: dict = {}

    head = tk.Frame(root, bg=HEAD, padx=24, pady=18)
    head.pack(fill="x")
    try:
        logo = tk.PhotoImage(file=str(ASSETS / "icon.png")).subsample(4)  # 64 px
        tk.Label(head, image=logo, bg=HEAD).pack(side="left", padx=(0, 14))
        head.logo = logo
    except tk.TclError:
        pass
    titles = tk.Frame(head, bg=HEAD)
    titles.pack(side="left", fill="x")
    _label(titles, "Set up Meeting Captions", 15, bold=True)
    _label(titles, "Takes a minute. You can switch meetings later from the tray icon.", 10, MUTED)

    body = tk.Frame(root, bg=BG, padx=24, pady=8)
    body.pack(fill="x")

    def section(title, help_text):
        f = tk.Frame(body, bg=BG)
        f.pack(fill="x", pady=(12, 0))
        _label(f, title, 10, bold=True)
        _label(f, help_text, 9, MUTED)
        return f

    name_var, name_entry = _entry(section(
        "Your name",
        "Shown next to what you say in the captions, and used to assign your action items in Odoo. "
        "Use the same name as in Odoo."), str(defaults.get("user_name", "")))

    meeting_var, _ = _entry(section(
        "Meeting",
        "Everyone who uses the same meeting name sees each other's captions. Ask the organiser, "
        "for example daily or mazara-weekly."), str(defaults.get("meeting_id", "daily")))

    lang_box = section("Language you speak",
                       "Pick your language. Automatic detection can write the wrong language when several "
                       "people talk at once.")
    names = [n for n, _ in LANGUAGES]
    code = str(defaults.get("language", "it"))
    lang_var = tk.StringVar(value=next((n for n, c in LANGUAGES if c == code), "Italian"))
    ttk.Combobox(lang_box, textvariable=lang_var, values=names, state="readonly", font=(FONT, 11)).pack(fill="x", pady=(4, 0), ipady=3)

    calls = tk.Frame(body, bg=BG)
    calls.pack(fill="x", pady=(16, 0))
    loop_var = tk.BooleanVar(value=bool(defaults.get("capture_system_audio", False)))
    tk.Checkbutton(calls, text="Also caption the other side of calls", variable=loop_var, bg=BG, fg=INK,
                   activebackground=BG, selectcolor=BG, font=(FONT, 10, "bold"), anchor="w").pack(fill="x")
    _label(calls, "Turn this on if you join Teams, Zoom or Meet calls from this computer. What the computer plays "
                  "through its speakers is captioned as \"Remote-Call\", so remote participants appear in the "
                  "captions too. Only one person per meeting room should turn it on, otherwise remote speech is "
                  "captioned twice.", 9, MUTED, padx=(24, 0))

    # advanced: hub address, normally filled in by the installer
    adv = tk.Frame(body, bg=BG)
    adv_shown = tk.BooleanVar(value=False)
    toggle = tk.Label(body, text="▸ Advanced: hub address", bg=BG, fg=BLUE, cursor="hand2", font=(FONT, 9, "bold"), anchor="w")
    toggle.pack(fill="x", pady=(14, 0))
    _label(adv, "Filled in automatically. Change it only if the office admin tells you to.", 9, MUTED)
    row = tk.Frame(adv, bg=BG)
    row.pack(fill="x")
    ip_frame, port_frame = tk.Frame(row, bg=BG), tk.Frame(row, bg=BG)
    ip_frame.pack(side="left", fill="x", expand=True)
    port_frame.pack(side="left", padx=(10, 0))
    ip_var, _ = _entry(ip_frame, str(defaults.get("server_ip", "")))
    port_var, _ = _entry(port_frame, str(defaults.get("hub_port", 8085)))

    def flip(_=None):
        adv_shown.set(not adv_shown.get())
        toggle.configure(text=("▾" if adv_shown.get() else "▸") + " Advanced: hub address")
        (adv.pack(fill="x", after=toggle) if adv_shown.get() else adv.pack_forget())
    toggle.bind("<Button-1>", flip)

    error = tk.Label(body, text="", bg=BG, fg=ERROR, font=(FONT, 9), anchor="w", justify="left", wraplength=420)
    error.pack(fill="x", pady=(10, 0))

    def ok(_=None):
        name, meeting = name_var.get().strip(), meeting_var.get().strip()
        if not SPEAKER_RE.fullmatch(name):
            error.configure(text="Type your name: letters, digits, spaces, dots, - or _ (up to 40 characters).")
            name_entry.focus_set()
            return
        if not MEETING_RE.fullmatch(meeting):
            error.configure(text="The meeting name can use letters, digits, - and _ only, without spaces.")
            return
        try:
            port = int(port_var.get().strip() or 8085)
        except ValueError:
            error.configure(text="The hub port must be a number, usually 8085.")
            return
        if not ip_var.get().strip():
            error.configure(text="The hub address is empty. Open Advanced and type the address the admin gave you.")
            return
        result.update(dict(defaults, user_name=name, meeting_id=meeting, server_ip=ip_var.get().strip(), hub_port=port,
                           language=dict(LANGUAGES)[lang_var.get()], capture_system_audio=loop_var.get()))
        root.destroy()

    btns = tk.Frame(root, bg=BG, padx=24, pady=16)
    btns.pack(fill="x")
    _button(btns, "Start", ok, True).pack(side="right")
    _button(btns, "Cancel", root.destroy, False).pack(side="right", padx=(0, 8))
    root.bind("<Return>", ok)
    root.bind("<Escape>", lambda e: root.destroy())
    if not defaults.get("server_ip"):
        flip()
    name_entry.focus_set()
    root.eval("tk::PlaceWindow . center")
    root.mainloop()
    return result or None


def ask_meeting(parent: tk.Misc, current: str) -> str | None:
    """Small window to switch meeting; returns the new name or None."""
    win = tk.Toplevel(parent)
    win.title("Change meeting")
    win.configure(bg=BG, padx=24, pady=18)
    win.resizable(False, False)
    win.attributes("-topmost", True)
    set_window_icon(win)
    _label(win, "Change meeting", 13, bold=True)
    _label(win, "Everyone who uses the same meeting name sees each other's captions.", 9, MUTED, pady=(2, 0))
    var, entry = _entry(win, current)
    err = tk.Label(win, text="", bg=BG, fg=ERROR, font=(FONT, 9), anchor="w")
    err.pack(fill="x", pady=(6, 0))
    out: dict = {}

    def ok(_=None):
        v = var.get().strip()
        if not MEETING_RE.fullmatch(v):
            err.configure(text="Letters, digits, - and _ only, without spaces.")
            return
        out["v"] = v
        win.destroy()

    btns = tk.Frame(win, bg=BG)
    btns.pack(fill="x", pady=(12, 0))
    _button(btns, "Join", ok, True).pack(side="right")
    _button(btns, "Cancel", win.destroy, False).pack(side="right", padx=(0, 8))
    win.bind("<Return>", ok)
    win.bind("<Escape>", lambda e: win.destroy())
    entry.focus_set()
    entry.select_range(0, "end")
    win.grab_set()
    parent.wait_window(win)
    v = out.get("v")
    return v if v and v != current else None
