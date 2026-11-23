"""Always-on-top caption strip (tkinter). Thread-safe `push(frame)` from the hub link."""
from __future__ import annotations

import queue
import tkinter as tk
from typing import Callable

PALETTE = ["#4fc3f7", "#aed581", "#ffb74d", "#ba68c8", "#f06292", "#4db6ac", "#fff176", "#90a4ae"]
BG, FG, MUTED = "#1e1e1e", "#f2f2f2", "#8a8a8a"


class CaptionOverlay:
    def __init__(self, on_mute: Callable[[bool], None], lines: int = 3) -> None:
        self.on_mute, self.lines = on_mute, lines
        self._q: queue.Queue[dict] = queue.Queue()
        self._done: list[dict] = []
        self._partials: dict[str, dict] = {}
        self._colors: dict[str, str] = {}
        self._muted = False
        self._hidden = False

        root = self.root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True, "-alpha", 0.90)
        root.configure(bg=BG)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = int(sw * 0.6), 150
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{sh - h - 70}")

        bar = tk.Frame(root, bg=BG)
        bar.pack(fill="x", padx=8, pady=(6, 0))
        self.status = tk.Label(bar, text="connecting…", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.status.pack(side="left")
        tk.Button(bar, text="✕", command=root.destroy, **self._btn()).pack(side="right")
        self.hide_btn = tk.Button(bar, text="Hide", command=self._toggle_hide, **self._btn())
        self.hide_btn.pack(side="right", padx=4)
        self.mute_btn = tk.Button(bar, text="Mute mic", command=self._toggle_mute, **self._btn())
        self.mute_btn.pack(side="right", padx=4)
        for widget in (bar, self.status):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        self.text = tk.Text(root, bg=BG, fg=FG, bd=0, highlightthickness=0, wrap="word",
                            font=("Segoe UI", 14), state="disabled", cursor="arrow")
        self.text.pack(fill="both", expand=True, padx=12, pady=8)
        self.text.tag_configure("partial", foreground=MUTED, font=("Segoe UI", 14, "italic"))
        self.text.bind("<ButtonPress-1>", self._drag_start)
        self.text.bind("<B1-Motion>", self._drag_move)
        root.after(100, self._drain)

    @staticmethod
    def _btn() -> dict:
        return dict(bg="#333", fg=FG, activebackground="#444", activeforeground=FG, bd=0,
                    padx=8, pady=2, font=("Segoe UI", 9))

    # --- public, thread-safe -------------------------------------------------
    def push(self, frame: dict) -> None:
        self._q.put(frame)

    def set_status(self, text: str) -> None:
        self._q.put({"type": "_status", "text": text})

    def run(self) -> None:
        self.root.mainloop()

    # --- internals -------------------------------------------------------------
    def _toggle_mute(self) -> None:
        self._muted = not self._muted
        self.mute_btn.configure(text="Unmute" if self._muted else "Mute mic",
                                bg="#a33" if self._muted else "#333")
        self.on_mute(self._muted)

    def _toggle_hide(self) -> None:
        self._hidden = not self._hidden
        if self._hidden:
            self.text.pack_forget()
            self.root.geometry(f"{self.root.winfo_width()}x34")
        else:
            self.text.pack(fill="both", expand=True, padx=12, pady=8)
            self.root.geometry(f"{self.root.winfo_width()}x150")
        self.hide_btn.configure(text="Show" if self._hidden else "Hide")

    def _drag_start(self, e) -> None:
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_move(self, e) -> None:
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _color(self, speaker: str) -> str:
        if speaker not in self._colors:
            self._colors[speaker] = PALETTE[len(self._colors) % len(PALETTE)]
            self.text.tag_configure(f"sp:{speaker}", foreground=self._colors[speaker],
                                    font=("Segoe UI", 14, "bold"))
        return f"sp:{speaker}"

    def _drain(self) -> None:
        changed = False
        while True:
            try:
                f = self._q.get_nowait()
            except queue.Empty:
                break
            if f["type"] == "_status":
                self.status.configure(text=f["text"])
            elif f["type"] == "caption":
                if f["completed"]:
                    self._partials.pop(f["speaker"], None)
                    self._done = (self._done + [f])[-self.lines:]
                else:
                    self._partials[f["speaker"]] = f
                changed = True
        if changed:
            self._redraw()
        self.root.after(100, self._drain)

    def _redraw(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for f in self._done:
            self.text.insert("end", f"[{f['speaker']}] ", self._color(f["speaker"]))
            self.text.insert("end", f["text"] + "\n")
        for f in self._partials.values():
            self.text.insert("end", f"[{f['speaker']}] ", self._color(f["speaker"]))
            self.text.insert("end", f["text"] + "\n", "partial")
        self.text.see("end")
        self.text.configure(state="disabled")
