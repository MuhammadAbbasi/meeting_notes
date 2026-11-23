"""First-run configuration form and meeting switcher (tkinter)."""
from __future__ import annotations

import tkinter as tk
from tkinter import simpledialog, ttk

FIELDS = [("user_name", "Your name (as shown in captions)"), ("server_ip", "Hub server IP"),
          ("hub_port", "Hub port"), ("meeting_id", "Meeting id (letters, digits, - _)"),
          ("language", "Language (auto / it / en)")]


def ask_config(defaults: dict) -> dict | None:
    """Modal form; returns the config dict or None if cancelled."""
    root = tk.Tk()
    root.title("Office Meeting Agent – setup")
    root.resizable(False, False)
    vars_: dict[str, tk.Variable] = {}
    for row, (key, label) in enumerate(FIELDS):
        ttk.Label(root, text=label).grid(row=row, column=0, sticky="w", padx=10, pady=4)
        v = tk.StringVar(value=str(defaults.get(key, "")))
        ttk.Entry(root, textvariable=v, width=32).grid(row=row, column=1, padx=10, pady=4)
        vars_[key] = v
    loop = tk.BooleanVar(value=bool(defaults.get("capture_system_audio", False)))
    ttk.Checkbutton(root, text="Also capture what this PC plays (remote call participants, as 'Remote-Call')",
                    variable=loop).grid(row=len(FIELDS), column=0, columnspan=2, sticky="w", padx=10, pady=6)
    result: dict = {}

    def ok():
        cfg = dict(defaults)
        for key, v in vars_.items():
            cfg[key] = v.get().strip()
        if not cfg["user_name"] or not cfg["server_ip"] or not cfg["meeting_id"]:
            return
        try:
            cfg["hub_port"] = int(cfg["hub_port"] or 8085)
        except ValueError:
            return
        cfg["capture_system_audio"] = loop.get()
        result.update(cfg)
        root.destroy()

    btns = ttk.Frame(root)
    btns.grid(row=len(FIELDS) + 1, column=0, columnspan=2, pady=10)
    ttk.Button(btns, text="Start", command=ok).pack(side="left", padx=6)
    ttk.Button(btns, text="Cancel", command=root.destroy).pack(side="left", padx=6)
    root.bind("<Return>", lambda e: ok())
    root.eval("tk::PlaceWindow . center")
    root.mainloop()
    return result or None


def ask_meeting(parent: tk.Misc, current: str) -> str | None:
    value = simpledialog.askstring("Change meeting", "Meeting id:", initialvalue=current, parent=parent)
    value = (value or "").strip()
    return value if value and value != current else None
