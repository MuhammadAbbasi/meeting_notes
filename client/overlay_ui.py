"""Always-on-top caption strip (tkinter), bottom-right, half transparent. Thread-safe `push`/`call`."""
from __future__ import annotations

import queue
import tkinter as tk
from typing import Callable

# Material 3 dark (Google) tones, matching the dashboard and the live captions page
PALETTE = ["#A8C7FA", "#6DD58C", "#FFD666", "#F2B8B5", "#D7AEFB", "#7FCFFF", "#FFB683", "#C4C7C5"]
BG, FG, MUTED, ACCENT, ERROR_BG = "#1E1F20", "#E3E3E3", "#C4C7C5", "#A8C7FA", "#8C1D18"
FONT = ("Segoe UI", 11)


class CaptionOverlay:
    def __init__(self, on_pause: Callable[[bool], None], on_hide: Callable[[], None] | None = None,
                 on_finalize: Callable[[], None] | None = None, on_mute_mic: Callable[[bool], None] | None = None,
                 lines: int = 3, alpha: float = 0.5) -> None:
        self.on_pause, self.on_mute_mic, self.on_hide, self.on_finalize, self.lines = on_pause, on_mute_mic, on_hide, on_finalize, lines
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
        self.status = tk.Label(bar, text="Connecting to the hub…", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.status.pack(side="left")
        self.notes_btn = tk.Button(bar, text="Open notes", command=self._open_notes, **self._btn())
        self._notes_url: str | None = None
        self.hide_btn = tk.Button(bar, text="Hide", command=self.hide, **self._btn())
        self.hide_btn.pack(side="right")
        self.pause_btn = tk.Button(bar, text="Pause all", command=self._toggle_pause, **self._btn())
        self.pause_btn.pack(side="right", padx=4)
        self.mic_btn = tk.Button(bar, text="Mute mic", command=self._toggle_mic, **self._btn())
        self.mic_btn.pack(side="right", padx=4)
        self.end_btn = None
        if on_finalize:
            end_style = {**self._btn(), "fg": "#F2B8B5", "activebackground": ERROR_BG, "activeforeground": "#F9DEDC"}
            self.end_btn = tk.Button(bar, text="End meeting", command=on_finalize, **end_style)
            self.end_btn.pack(side="right", padx=4)
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
        self._mic_paused = False
        root.after(100, self._drain)

    @staticmethod
    def _btn() -> dict:
        # text buttons, Material style: accent label on the surface, a light tint when pressed
        return dict(bg=BG, fg=ACCENT, activebackground="#2D3440", activeforeground=ACCENT, bd=0, relief="flat",
                    cursor="hand2", padx=8, pady=2, font=("Segoe UI", 9, "bold"))

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
        self.pause_btn.configure(text="Resume all" if paused else "Pause all", bg=ERROR_BG if paused else BG,
                                 fg="#F9DEDC" if paused else ACCENT)

    def set_mic_paused(self, paused: bool) -> None:
        self._mic_paused = paused
        self.mic_btn.configure(text="Unmute mic" if paused else "Mute mic", bg=ERROR_BG if paused else BG,
                               fg="#F9DEDC" if paused else ACCENT)

    def set_running(self, running: bool) -> None:
        def apply():
            if running:
                self.notes_btn.pack_forget()
                self.pause_btn.pack(side="right", padx=4, before=self.hide_btn)
                self.mic_btn.pack(side="right", padx=4, before=self.pause_btn)
                if self.end_btn is not None:
                    self.end_btn.pack(side="right", padx=4, before=self.mic_btn)
            else:
                self.pause_btn.pack_forget()
                self.mic_btn.pack_forget()
                if self.end_btn is not None:
                    self.end_btn.pack_forget()
        self.call(apply)

    def show_notes_link(self, url: str) -> None:
        self._notes_url = url
        self.call(lambda: self.notes_btn.pack(side="right", padx=4))

    def _open_notes(self) -> None:
        if self._notes_url:
            import webbrowser
            webbrowser.open(self._notes_url)

    def _toggle_pause(self) -> None:
        self.on_pause(not self._paused)

    def _toggle_mic(self) -> None:
        if self.on_mute_mic:
            self.on_mute_mic(not self._mic_paused)

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
            elif f["type"] == "retract":
                self._partials.pop(f["speaker"], None)
                changed = True
            elif f["type"] == "error":
                self.status.configure(text=f["message"])
            elif f["type"] == "finalized":
                self.status.configure(text="Minutes written" if f["llm"] else "Only the transcript was saved: the minutes writer isn't available")
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
