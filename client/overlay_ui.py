"""Always-on-top caption strip (tkinter), bottom-right, half transparent. Thread-safe `push`/`call`."""
from __future__ import annotations

import queue
import tkinter as tk
from typing import Callable

PALETTE = ["#4fc3f7", "#aed581", "#ffb74d", "#ba68c8", "#f06292", "#4db6ac", "#fff176", "#90a4ae"]
BG, FG, MUTED = "#1e1e1e", "#f2f2f2", "#8a8a8a"
FONT = ("Segoe UI", 11)


class CaptionOverlay:
    def __init__(self, on_pause: Callable[[bool], None], on_hide: Callable[[], None] | None = None,
                 lines: int = 3, alpha: float = 0.5) -> None:
        self.on_pause, self.on_hide, self.lines = on_pause, on_hide, lines
        self._q: queue.Queue = queue.Queue()
        self._done: list[dict] = []
        self._partials: dict[str, dict] = {}
        self._colors: dict[str, str] = {}
        self.visible = True

        root = self.root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True, "-alpha", alpha)
        root.configure(bg=BG)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.w, self.h = max(380, int(sw * 0.28)), 120
        root.geometry(f"{self.w}x{self.h}+{sw - self.w - 16}+{sh - self.h - 60}")

        bar = tk.Frame(root, bg=BG)
        bar.pack(fill="x", padx=6, pady=(4, 0))
        self.status = tk.Label(bar, text="connecting…", bg=BG, fg=MUTED, font=("Segoe UI", 8))
        self.status.pack(side="left")
        tk.Button(bar, text="Hide", command=self.hide, **self._btn()).pack(side="right")
        self.pause_btn = tk.Button(bar, text="Pause", command=self._toggle_pause, **self._btn())
        self.pause_btn.pack(side="right", padx=4)
        for widget in (bar, self.status):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        self.text = tk.Text(root, bg=BG, fg=FG, bd=0, highlightthickness=0, wrap="word",
                            font=FONT, state="disabled", cursor="arrow")
        self.text.pack(fill="both", expand=True, padx=10, pady=6)
        self.text.tag_configure("partial", foreground=MUTED, font=(*FONT, "italic"))
        self.text.bind("<ButtonPress-1>", self._drag_start)
        self.text.bind("<B1-Motion>", self._drag_move)
        self._paused = False
        root.after(100, self._drain)

    @staticmethod
    def _btn() -> dict:
        return dict(bg="#333", fg=FG, activebackground="#444", activeforeground=FG, bd=0,
                    padx=6, pady=1, font=("Segoe UI", 8))

    # --- public, thread-safe -------------------------------------------------
    def push(self, frame: dict) -> None:
        self._q.put(frame)

    def set_status(self, text: str) -> None:
        self._q.put({"type": "_status", "text": text})

    def call(self, fn: Callable[[], None]) -> None:
        """Run `fn` on the Tk thread (for tray callbacks)."""
        self._q.put({"type": "_call", "fn": fn})

    def run(self) -> None:
        self.root.mainloop()

    # --- Tk-thread only --------------------------------------------------------
    def show(self) -> None:
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.visible = True

    def hide(self) -> None:
        self.root.withdraw()
        self.visible = False
        if self.on_hide:
            self.on_hide()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.pause_btn.configure(text="Resume" if paused else "Pause", bg="#a33" if paused else "#333")

    def _toggle_pause(self) -> None:
        self.on_pause(not self._paused)

    def _drag_start(self, e) -> None:
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_move(self, e) -> None:
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _color(self, speaker: str) -> str:
        if speaker not in self._colors:
            self._colors[speaker] = PALETTE[len(self._colors) % len(PALETTE)]
            self.text.tag_configure(f"sp:{speaker}", foreground=self._colors[speaker], font=(*FONT, "bold"))
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
            elif f["type"] == "_call":
                f["fn"]()
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
