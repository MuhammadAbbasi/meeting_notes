"""First-run setup window and meeting switcher (tkinter), in the same Google style as the dashboard."""
from __future__ import annotations

import re
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk

from hub_link import match_odoo_user

ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "assets"
SPEAKER_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,40}$")  # same rule as the hub (CONTRACTS 1.1)
MEETING_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LANGUAGES = [("Italiano", "it"), ("English", "en"), ("Deutsch", "de"), ("Français", "fr"), ("Español", "es"),
             ("Rileva automaticamente", "auto")]
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
    root.title("Meeting Captions – Configurazione")
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
    _label(titles, "Configura Meeting Captions", 15, bold=True)
    _label(titles, "Richiede un minuto. Puoi cambiare riunione in seguito dall'icona vicino all'orologio.", 10, MUTED)

    body = tk.Frame(root, bg=BG, padx=24, pady=8)
    body.pack(fill="x")

    def section(title, help_text):
        f = tk.Frame(body, bg=BG)
        f.pack(fill="x", pady=(12, 0))
        _label(f, title, 10, bold=True)
        _label(f, help_text, 9, MUTED)
        return f

    name_var, name_entry = _entry(section(
        "Nome e cognome",
        "Inserisci nome e cognome (es. Muhammad Abbasi). Mostrato nei sottotitoli e abbinato a Odoo."),
        str(defaults.get("user_name", "")))

    email_var, email_entry = _entry(section(
        "Email aziendale",
        "Abbinata a Odoo per assegnare automaticamente le tue attività."), str(defaults.get("user_email", "")))

    default_mid = defaults.get("meeting_id")
    if not default_mid or default_mid == "daily":
        default_mid = datetime.now().strftime("%Y-%m-%d_%H%M")
    meeting_var, _ = _entry(section(
        "Riunione",
        "Chi usa lo stesso nome riunione vede i sottotitoli condivisi. Predefinito: data_ora."), str(default_mid))

    lang_box = section("Lingua parlata",
                       "Scegli la lingua della riunione (predefinito: Italiano).")
    names = [n for n, _ in LANGUAGES]
    code = str(defaults.get("language", "it"))
    lang_var = tk.StringVar(value=next((n for n, c in LANGUAGES if c == code), "Italiano"))
    ttk.Combobox(lang_box, textvariable=lang_var, values=names, state="readonly", font=(FONT, 11)).pack(fill="x", pady=(4, 0), ipady=3)

    calls = tk.Frame(body, bg=BG)
    calls.pack(fill="x", pady=(16, 0))
    loop_var = tk.BooleanVar(value=bool(defaults.get("capture_system_audio", True)))
    tk.Checkbutton(calls, text="Sottotitola anche i partecipanti remoti (call)", variable=loop_var, bg=BG, fg=INK,
                   activebackground=BG, selectcolor=BG, font=(FONT, 10, "bold"), anchor="w").pack(fill="x")
    _label(calls, "Attivalo per acquisire l'audio di Teams, Zoom o Meet (etichettato come \"Remote-1\", \"Remote-2\"). "
                  "Se più colleghi sono nella stessa stanza, il server gestisce automaticamente un solo flusso remoto.", 9, MUTED, padx=(24, 0))

    # advanced: hub address, normally filled in by the installer
    adv = tk.Frame(body, bg=BG)
    adv_shown = tk.BooleanVar(value=False)
    toggle = tk.Label(body, text="▸ Avanzate: indirizzo server", bg=BG, fg=BLUE, cursor="hand2", font=(FONT, 9, "bold"), anchor="w")
    toggle.pack(fill="x", pady=(14, 0))
    _label(adv, "Compilato in automatico. Modificalo solo se indicato dall'amministratore.", 9, MUTED)
    row = tk.Frame(adv, bg=BG)
    row.pack(fill="x")
    ip_frame, port_frame = tk.Frame(row, bg=BG), tk.Frame(row, bg=BG)
    ip_frame.pack(side="left", fill="x", expand=True)
    port_frame.pack(side="left", padx=(10, 0))
    ip_var, _ = _entry(ip_frame, str(defaults.get("server_ip", "")))
    port_var, _ = _entry(port_frame, str(defaults.get("hub_port", 8085)))

    def flip(_=None):
        adv_shown.set(not adv_shown.get())
        toggle.configure(text=("▾" if adv_shown.get() else "▸") + " Avanzate: indirizzo server")
        (adv.pack(fill="x", after=toggle) if adv_shown.get() else adv.pack_forget())
    toggle.bind("<Button-1>", flip)

    error = tk.Label(body, text="", bg=BG, fg=ERROR, font=(FONT, 9), anchor="w", justify="left", wraplength=420)
    error.pack(fill="x", pady=(10, 0))

    def ok(_=None):
        name, email, meeting = name_var.get().strip(), email_var.get().strip(), meeting_var.get().strip()
        if not SPEAKER_RE.fullmatch(name) or len(name.split()) < 2:
            error.configure(text="Inserisci sia il nome che il cognome (es. Muhammad Abbasi).")
            name_entry.focus_set()
            return
        if "@" not in email or "." not in email.split("@", 1)[-1]:
            error.configure(text="Inserisci la tua email aziendale (es. nome@a176lab.it).")
            email_entry.focus_set()
            return
        if not MEETING_RE.fullmatch(meeting):
            error.configure(text="Il nome riunione può contenere solo lettere, numeri, - e _, senza spazi.")
            return
        try:
            port = int(port_var.get().strip() or 8085)
        except ValueError:
            error.configure(text="La porta del server deve essere un numero (di solito 8085).")
            return
        ip = ip_var.get().strip()
        if not ip:
            error.configure(text="L'indirizzo del server è vuoto. Apri Avanzate per inserirlo.")
            return
        matched, canonical_name, msg = match_odoo_user(ip, port, name, email)
        if not matched:
            error.configure(text=msg)
            email_entry.focus_set()
            return
        dev_id = None
        result.update(dict(defaults, user_name=canonical_name, user_email=email, meeting_id=meeting, server_ip=ip,
                           hub_port=port, input_device=dev_id, language=dict(LANGUAGES)[lang_var.get()],
                           capture_system_audio=loop_var.get()))
        root.destroy()

    btns = tk.Frame(root, bg=BG, padx=24, pady=16)
    btns.pack(fill="x")
    _button(btns, "Salva e avvia", ok, True).pack(side="right")
    _button(btns, "Annulla", root.destroy, False).pack(side="right", padx=(0, 8))
    root.bind("<Return>", ok)
    root.bind("<Escape>", lambda e: root.destroy())
    if not defaults.get("server_ip"):
        flip()
    name_entry.focus_set()
    root.eval("tk::PlaceWindow . center")
    root.mainloop()
    return result or None


def _format_meeting_choice(m: dict) -> str:
    title = m.get("title") or m["meeting_id"]
    extra = []
    if m.get("start"):
        extra.append(m["start"][11:16])
    if m.get("live_speakers"):
        extra.append(f"{len(m['live_speakers'])} live")
    return f"{m['meeting_id']}  —  {title}" + (f" ({', '.join(extra)})" if extra else "")


def ask_meeting(parent: tk.Misc, current: str, meetings: list[dict] | None = None,
                current_lang: str = "it", allow_same: bool = False) -> tuple[str, str] | None:
    """Google-styled meeting switcher with Odoo calendar + language; returns (meeting_id, language) or None."""
    win = tk.Toplevel(parent)
    win.title("Avvia o partecipa a riunione")
    win.configure(bg=BG, padx=24, pady=18)
    win.resizable(False, False)
    win.attributes("-topmost", True)
    set_window_icon(win)
    _label(win, "Avvia o partecipa a riunione", 13, bold=True)
    _label(win, "Scegli una riunione dal calendario Odoo o scrivi un nome.", 9, MUTED, pady=(2, 6))
    choices = [_format_meeting_choice(m) for m in (meetings or [])]
    var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d_%H%M") if current in ("", "daily") or allow_same else current)
    if choices:
        cb = ttk.Combobox(win, textvariable=var, values=choices, font=(FONT, 11))
        cb.pack(fill="x", ipady=4, pady=(4, 0))
        entry = cb
    else:
        var, entry = _entry(win, current)
    _label(win, "Lingua riunione", 9, MUTED, pady=(10, 0))
    lang_names = [n for n, _ in LANGUAGES]
    lang_var = tk.StringVar(value=next((n for n, c in LANGUAGES if c == current_lang), "Italiano"))
    ttk.Combobox(win, textvariable=lang_var, values=lang_names, state="readonly", font=(FONT, 11)).pack(fill="x", pady=(4, 0), ipady=3)
    err = tk.Label(win, text="", bg=BG, fg=ERROR, font=(FONT, 9), anchor="w")
    err.pack(fill="x", pady=(6, 0))
    out: dict = {}

    def ok(_=None):
        v = var.get().split("  —  ", 1)[0].strip()
        if not MEETING_RE.fullmatch(v):
            err.configure(text="Solo lettere, numeri, - e _, senza spazi.")
            return
        out["v"] = v
        out["lang"] = dict(LANGUAGES)[lang_var.get()]
        win.destroy()

    btns = tk.Frame(win, bg=BG)
    btns.pack(fill="x", pady=(12, 0))
    _button(btns, "Partecipa", ok, True).pack(side="right")
    _button(btns, "Annulla", win.destroy, False).pack(side="right", padx=(0, 8))
    win.bind("<Return>", ok)
    win.bind("<Escape>", lambda e: win.destroy())
    entry.focus_set()
    win.grab_set()
    parent.wait_window(win)
    v = out.get("v")
    lang = out.get("lang", current_lang)
    return (v, lang) if v and (allow_same or v != current or lang != current_lang) else None


def ask_start_scheduled(parent: tk.Misc, title: str) -> bool:
    """Prompt when a scheduled Odoo calendar meeting begins."""
    win = tk.Toplevel(parent)
    win.title("Riunione in arrivo")
    win.configure(bg=BG, padx=24, pady=20)
    win.resizable(False, False)
    win.attributes("-topmost", True)
    set_window_icon(win)
    _label(win, f"Avviare i sottotitoli per “{title}”?", 13, bold=True)
    _label(win, "Questa riunione dal calendario Odoo sta iniziando ora. Vuoi avviare i sottotitoli?", 10, MUTED, pady=(6, 4))
    out = {"start": False}

    def go(val: bool):
        out["start"] = val
        win.destroy()

    btns = tk.Frame(win, bg=BG)
    btns.pack(fill="x", pady=(14, 0))
    _button(btns, "Avvia sottotitoli", lambda: go(True), True).pack(side="right")
    _button(btns, "Non ora", lambda: go(False), False).pack(side="right", padx=(0, 8))
    win.bind("<Return>", lambda e: go(True))
    win.bind("<Escape>", lambda e: go(False))
    win.grab_set()
    parent.wait_window(win)
    return out["start"]


def ask_end_meeting(parent: tk.Misc, meeting_id: str, reason: str = "manual") -> str:
    """Confirm stopping the tool and generating meeting notes. Returns 'finalize', 'stop', or 'cancel'."""
    reasons = {
        "empty": "Tutti gli altri partecipanti hanno lasciato la riunione.",
        "idle": "Nessun parlato rilevato negli ultimi 10 minuti.",
        "scheduled": "L'orario previsto su Odoo per questa riunione è terminato.",
    }
    win = tk.Toplevel(parent)
    win.title("Termina riunione")
    win.configure(bg=BG, padx=24, pady=20)
    win.resizable(False, False)
    win.attributes("-topmost", True)
    set_window_icon(win)
    _label(win, f"Terminare la riunione “{meeting_id}”?", 13, bold=True)
    msg = (f"{reasons[reason]} " if reason in reasons else "") + "Vuoi fermare lo strumento e generare il verbale?"
    _label(win, msg, 10, MUTED, pady=(6, 4))
    out = {"choice": "cancel"}

    def pick(choice: str):
        out["choice"] = choice
        win.destroy()

    btns = tk.Frame(win, bg=BG)
    btns.pack(fill="x", pady=(14, 0))
    _button(btns, "Ferma e genera", lambda: pick("finalize"), True).pack(side="right")
    _button(btns, "Ferma soltanto", lambda: pick("stop"), False).pack(side="right", padx=(0, 6))
    _button(btns, "Annulla", lambda: pick("cancel"), False).pack(side="right", padx=(0, 6))
    win.bind("<Return>", lambda e: pick("finalize"))
    win.bind("<Escape>", lambda e: pick("cancel"))
    win.grab_set()
    parent.wait_window(win)
    return out["choice"]
