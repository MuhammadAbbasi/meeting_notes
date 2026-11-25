"""Generate a ~10-minute, 10-person bilingual meeting about a PV panel-cleaning problem, as audio.

    python tests/meeting_panel_cleaning.py OUT_DIR

Writes, in OUT_DIR:
  mic_<Name>.wav       one 16 kHz mono track per person: their own voice, plus a little of everyone else
                       (same-room bleed, BLEED gain) - what each person's agent would send
  meeting_mix.wav      everyone mixed, to listen to
  reference.json       who said what and when, for scoring the captions
Voices: Windows desktop voices (System.Speech), varied in rate and pitch so ten people sound different.
Then run, for example:
  python tests/simulate_multi_user_meeting.py --hub ws://HUB:8085 --meeting panel-cleaning-10 \
      --speakers <names> --wav <mic files> --langs <langs> --noise 0.01 --finalize
  python tests/score_meeting.py --hub http://HUB:8085 --meeting panel-cleaning-10 --reference OUT_DIR/reference.json
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

RATE = 16000
BLEED = float(__import__("os").getenv("MEETING_BLEED", "0.05"))  # how much of the room each microphone also picks up (0 = headsets)

# name: (language, Windows voice, rate -10..10, pitch change)
PEOPLE = {
    "Muhammad": ("en", "Microsoft David Desktop", 0, "+0%"),
    "Gabriele": ("it", "Microsoft Cosimo", 0, "+0%"),
    "Ginadomenico": ("it", "Microsoft Cosimo", -2, "-15%"),
    "Anna": ("it", "Microsoft Elsa Desktop", 0, "+0%"),
    "Sara": ("it", "Microsoft Elsa Desktop", 2, "+15%"),
    "Paolo": ("it", "Microsoft Cosimo", 2, "+12%"),
    "John": ("en", "Microsoft Mark", 0, "+0%"),
    "Emily": ("en", "Microsoft Zira Desktop", 0, "+0%"),
    "Sophie": ("en", "Microsoft Zira Desktop", 2, "+15%"),
    "Luca": ("en", "Microsoft David Desktop", -2, "-15%"),
}

# (speaker, text, overlap): overlap = seconds this turn starts before the previous one ends (interruption)
SCRIPT = [
    ("Muhammad", "Good morning everyone, thanks for joining. Today we have one topic: the panel cleaning problem at Mazara. The performance ratio dropped again last month and we need a plan we can start this week.", 0),
    ("Gabriele", "Buongiorno a tutti. Confermo, il PR di ottobre è sceso all'ottantuno per cento, contro l'ottantasei di settembre.", 0),
    ("Anna", "Ho confrontato i dati con l'irraggiamento. La perdita non dipende dal meteo, è sporcamento dei moduli.", 0),
    ("Emily", "Before we go further, can someone tell me what that drop means in money?", 0),
    ("Gabriele", "Circa diciottomila euro al mese di energia persa, se continua così.", 0),
    ("Emily", "That is a lot. Okay, go on.", 0),
    ("Ginadomenico", "Il problema principale è la polvere della cava vicina. Con il vento da sud i moduli della zona nord si sporcano in pochi giorni.", 0),
    ("Sara", "E da quando non piove, lo sporco si attacca ancora di più. L'ultima pulizia è stata ad agosto.", 0),
    ("Muhammad", "So the cleaning interval is simply too long for this site.", 0),
    ("Paolo", "Esatto, e la squadra esterna può venire solo una volta ogni tre mesi con il contratto attuale.", 0),
    ("John", "I spoke with two robot suppliers last week. Dry cleaning robots could clean the north blocks every night without water.", 0),
    ("Luca", "Before we talk about robots, I want to raise safety. The manual cleaning crews were working near live strings without proper isolation.", 0),
    ("Muhammad", "Good point, Luca. Safety comes first, we will come back to it.", 1.0),
    ("Sophie", "From the warranty side, the module manufacturer only accepts soft brushes and demineralised water. Some robots use hard brushes.", 0),
    ("Anna", "Sì, ho letto il manuale. Spazzole morbide e niente detergenti.", 0),
    ("Gabriele", "Allora io proporrei una soluzione mista: robot a secco sulla zona nord, e lavaggio con acqua demineralizzata due volte all'anno su tutto l'impianto.", 0),
    ("John", "That matches what the suppliers suggested. The robots cost about two hundred euros per row.", 0),
    ("Emily", "How many rows are we talking about?", 0),
    ("Ginadomenico", "Nella zona nord ci sono centoventi file.", 0),
    ("Emily", "So twenty four thousand euros for the robots, plus installation.", 0),
    ("Paolo", "Più la manutenzione dei robot. Le batterie vanno cambiate ogni quattro anni circa.", 0),
    ("Emily", "With eighteen thousand a month in losses, the payback is less than two months for the north area. I can support that.", 0),
    ("Sara", "Ma attenzione: i robot non risolvono gli escrementi degli uccelli. Quelli servono il lavaggio.", 0),
    ("Muhammad", "Right. So we still need the wet cleaning. What about water?", 0),
    ("Gabriele", "L'acqua è il vero problema. Il pozzo ha poca portata in estate.", 0),
    ("Anna", "Possiamo noleggiare un'autobotte con acqua demineralizzata. Costa circa tre euro al metro cubo.", 0),
    ("Luca", "And the crew must follow the lockout procedure. I can prepare a short safety briefing for the contractor.", 0),
    ("Sophie", "Please also make sure the contractor signs the method statement. Otherwise the insurance will not cover damage to modules.", 0),
    ("Paolo", "Va bene. Io posso chiedere tre preventivi per il lavaggio entro venerdì.", 0),
    ("Muhammad", "Thank you Paolo. Three quotes by Friday.", 0),
    ("John", "I will ask both robot suppliers for a pilot. Maybe we can test ten rows for one month.", 0),
    ("Ginadomenico", "Il pilota lo farei sulle file vicino alla cava, dove lo sporco è peggiore.", 0),
    ("Gabriele", "D'accordo. Così vediamo subito la differenza.", 0.6),
    ("Anna", "Io posso preparare il confronto: dieci file con robot, dieci file senza, stessi inverter.", 0),
    ("Sophie", "Good, then we have clean data for the warranty discussion too.", 0),
    ("Emily", "What does the pilot cost?", 0),
    ("John", "The suppliers usually lend the robots for free for a month if we pay installation, around one thousand five hundred euros.", 0),
    ("Emily", "Fine, that fits in the operations budget.", 0),
    ("Sara", "Un'altra cosa: le stringhe della zona nord hanno anche dei moduli danneggiati. Li ho visti con la termografia.", 0),
    ("Muhammad", "How many?", 0),
    ("Sara", "Sei moduli con hotspot, probabilmente per lo sporco accumulato sul bordo.", 0),
    ("Gabriele", "Quelli vanno sostituiti prima del lavaggio, altrimenti rischiamo.", 0),
    ("Luca", "Agreed, damaged modules must be isolated before anyone washes that area.", 0),
    ("Paolo", "Abbiamo sei moduli di ricambio in magazzino. Posso sostituirli la settimana prossima.", 0),
    ("Muhammad", "Perfect. Paolo replaces the six modules next week, before any cleaning.", 0),
    ("Ginadomenico", "Io parlerei anche con la cava. Forse possono bagnare la strada sterrata quando c'è vento.", 0),
    ("Anna", "Buona idea, ridurrebbe la polvere alla fonte.", 0),
    ("Muhammad", "Ginadomenico, can you contact them this week?", 0),
    ("Ginadomenico", "Sì, li chiamo domani mattina.", 0),
    ("Sophie", "If they agree, let's get it in writing. A simple letter is enough.", 0),
    ("John", "One more thing on robots: they need a small communication network. I can check if our SCADA can monitor them.", 0),
    ("Anna", "Lo SCADA ha ancora porte libere sul quadro nord, l'ho verificato il mese scorso.", 0),
    ("Gabriele", "E dobbiamo aggiungere un allarme se un robot si blocca su una fila.", 0),
    ("John", "Yes, the suppliers have an alarm output. I will ask for the documentation.", 0),
    ("Emily", "Let me summarise the budget. Pilot fifteen hundred, wet cleaning quotes pending, robots twenty four thousand if the pilot works.", 0),
    ("Sara", "E i sei moduli sono già in magazzino, quindi costo zero.", 0),
    ("Emily", "Exactly. I will prepare the budget request for the board by the end of the month.", 0),
    ("Luca", "I also need the contractor's training certificates before they start. Paolo, please ask for them with the quotes.", 0),
    ("Paolo", "Certo, li chiedo insieme ai preventivi.", 0),
    ("Muhammad", "How do we measure success? I want a clear number.", 0),
    ("Anna", "Direi recuperare almeno quattro punti di PR nella zona nord entro due mesi.", 0),
    ("Gabriele", "Sì, e lo misuriamo ogni settimana con il report del tracker.", 0),
    ("Sophie", "And keep the photos before and after cleaning. The manufacturer will ask for them.", 0),
    ("Sara", "Le foto le faccio io con il drone, prima e dopo.", 0),
    ("John", "Should we also think about the cleaning calendar for next year? Summer is when we lose the most.", 0),
    ("Gabriele", "Sì. Io proporrei un lavaggio a fine maggio, prima del picco estivo, e uno a fine settembre.", 0),
    ("Ginadomenico", "E uno in più dopo la vendemmia, perché i camion sollevano tantissima polvere sulla strada.", 0),
    ("Emily", "Three washes a year instead of four. Does that still work with the robots?", 0),
    ("Anna", "Con i robot sulla zona nord, sì. Nelle altre zone lo sporco è molto più lento.", 0),
    ("Sophie", "Can we reuse the washing water? Water is expensive and the municipality may ask questions.", 0),
    ("Paolo", "Con l'acqua demineralizzata non conviene recuperarla, si sporca subito. Meglio usarne meno con lance a bassa pressione.", 0),
    ("Luca", "Low pressure is also safer, less risk of water reaching the junction boxes.", 0),
    ("Sara", "Io intanto posso fare un volo con il drone ogni mese, così vediamo dove si accumula lo sporco.", 0),
    ("Muhammad", "Monthly drone flights, that's useful. Can the drone images be compared automatically?", 0),
    ("Anna", "Sì, lo script della termografia può confrontare le immagini e segnare le file più sporche.", 0),
    ("John", "Then the robots could clean those rows first. The suppliers' software supports custom schedules.", 0),
    ("Gabriele", "Perfetto, così il lavaggio diventa su richiesta e non solo a calendario.", 0),
    ("Emily", "That would reduce cost further. I like it.", 0),
    ("Ginadomenico", "Però serve qualcuno che guardi i dati ogni settimana, altrimenti nessuno se ne accorge.", 0),
    ("Muhammad", "Agreed. Gabriele, the weekly PR report can include the drone results as well.", 0),
    ("Gabriele", "Va bene, li aggiungo al report.", 0),
    ("Sophie", "And please keep the cleaning log in Odoo, one task per cleaning, so we have history for the warranty.", 0),
    ("Paolo", "Sì, creo i task in Odoo dopo ogni intervento.", 0),
    ("Muhammad", "Great. Any objections to the plan?", 0),
    ("Ginadomenico", "Nessuna obiezione.", 0),
    ("John", "None from me.", 0.5),
    ("Emily", "Agreed, as long as the pilot results come before the board meeting.", 0),
    ("Luca", "Agreed, with the safety briefing first.", 0),
    ("Muhammad", "Then let me repeat the actions so everyone is clear.", 0),
    ("Muhammad", "Paolo: three wet cleaning quotes and the training certificates by Friday, and the six modules replaced next week.", 0),
    ("Muhammad", "John: robot pilot on ten rows near the quarry, installation within two weeks, and the alarm documentation.", 0),
    ("Muhammad", "Anna: comparison of ten cleaned rows against ten reference rows, weekly.", 0),
    ("Muhammad", "Ginadomenico: call the quarry tomorrow about wetting the road.", 0),
    ("Muhammad", "Sara: drone photos before and after. Luca: safety briefing before any work. Emily: budget request by the end of the month.", 0),
    ("Gabriele", "E io aggiorno il report settimanale del PR con la zona nord separata.", 0),
    ("Muhammad", "Thank you Gabriele. Sophie, anything for the warranty?", 0),
    ("Sophie", "I will send the manufacturer our cleaning method and ask them to confirm it in writing.", 0),
    ("Muhammad", "Perfect. We meet again in two weeks to review the pilot. Thank you all.", 0),
    ("Anna", "Grazie, buona giornata.", 0),
    ("Paolo", "Grazie a tutti.", 0.3),
]


def synthesize(out: Path) -> list[Path]:
    """One WAV per line, made by a single PowerShell run (System.Speech, 16 kHz mono)."""
    out.mkdir(parents=True, exist_ok=True)
    lines = ["Add-Type -AssemblyName System.Speech",
             "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
             "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)",
             "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer"]
    files = []
    for i, (who, text, _) in enumerate(SCRIPT):
        lang, voice, rate, pitch = PEOPLE[who]
        f = out / f"utt_{i:03d}.wav"
        files.append(f)
        xml_lang = "it-IT" if lang == "it" else "en-US"
        safe = text.replace("&", "and").replace("<", "").replace(">", "").replace("'", "&apos;")
        ssml = (f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='{xml_lang}'>"
                f"<voice name='{voice}'><prosody pitch='{pitch}'>{safe}</prosody></voice></speak>")
        lines += [f"$s.Rate = {rate}", f"$s.SetOutputToWaveFile('{f}', $fmt)",
                  "$s.SpeakSsml(@'\n" + ssml + "\n'@)", "$s.SetOutputToNull()"]
    script = out / "synth.ps1"
    script.write_text("\n".join(lines), encoding="utf-8-sig")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], check=True)
    return files


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


def write_wav(path: Path, pcm: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes())


def build(out: Path) -> None:
    rng = random.Random(42)
    clips = [read_wav(f) for f in synthesize(out)]
    t, prev_end, placed = 1.0, 1.0, []
    for (who, text, overlap), clip in zip(SCRIPT, clips):
        start = max(0.5, prev_end - overlap) if overlap else t
        end = start + len(clip) / RATE
        placed.append((who, text, start, end, clip))
        prev_end = end
        t = end + rng.uniform(0.35, 1.1)
    total = int((max(p[3] for p in placed) + 1.5) * RATE)
    tracks = {who: np.zeros(total, np.float32) for who in PEOPLE}
    for who, _, start, _, clip in placed:
        i = int(start * RATE)
        tracks[who][i:i + len(clip)] += clip
    mix = sum(tracks.values())
    for who, own in tracks.items():
        write_wav(out / f"mic_{who}.wav", own + BLEED * (mix - own))
    peak = float(np.abs(mix).max()) or 1.0
    write_wav(out / "meeting_mix.wav", mix / peak * 0.9)
    ref = [{"speaker": w, "lang": PEOPLE[w][0], "text": tx, "start": round(s, 2), "end": round(e, 2)} for w, tx, s, e, _ in placed]
    (out / "reference.json").write_text(json.dumps(ref, ensure_ascii=False, indent=1), encoding="utf-8")
    for f in out.glob("utt_*.wav"):
        f.unlink()
    words = sum(len(r["text"].split()) for r in ref)
    print(f"{len(ref)} turns, {words} words, {total / RATE / 60:.1f} min, 10 microphones + mix in {out}")
    print("speakers:", ",".join(PEOPLE))
    print("langs:   ", ",".join(v[0] for v in PEOPLE.values()))


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "meeting_out"))
