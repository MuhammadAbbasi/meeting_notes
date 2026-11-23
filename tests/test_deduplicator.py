from server.deduplicator import CrossTalkFilter, normalize_text, text_similarity


def frame(speaker, text, ts, rms, start=0.0):
    return {"type": "caption", "meeting_id": "m", "speaker": speaker, "text": text, "completed": True,
            "start": start, "end": start + 2.0, "ts": ts, "rms": rms}


def test_normalize_text():
    assert normalize_text("  Il Tracker, T12: ha PERSO!  ") == "il tracker t12 ha perso"


def test_similarity_handles_stt_noise():
    a = "il tracker T12 ha perso la comunicazione per due ore"
    b = "il tracker t 12 ha perso la comunicazione, per due ore."
    assert text_similarity(a, b) >= 0.9
    assert text_similarity("we should update the PR template", "I will export the VCOM data") < 0.5


def test_containment_counts_as_similar():
    full = "propongo di aggiungere un controllo automatico sui log SCADA ogni mattina"
    bleed = "aggiungere un controllo automatico sui log"
    assert text_similarity(full, bleed) == 1.0


def test_quieter_duplicate_suppressed_either_order():
    text = "ieri il tracker T12 ha perso la comunicazione per due ore"
    loud, quiet = frame("Muhammad", text, 1000.0, 0.08), frame("Gabriele", text + ".", 1000.9, 0.01)
    f = CrossTalkFilter()
    f.add(loud); f.add(quiet)
    assert f.should_emit(loud) is True and f.should_emit(quiet) is False
    g = CrossTalkFilter()
    g.add(quiet); g.add(loud)  # bleed arrives first
    assert g.should_emit(quiet) is False and g.should_emit(loud) is True


def test_equal_rms_keeps_first_arrival():
    text = "chiudiamo con le azioni e le scadenze di oggi"
    a, b = frame("A", text, 1000.0, 0.05), frame("B", text, 1000.5, 0.05)
    f = CrossTalkFilter()
    f.add(a); f.add(b)
    assert f.should_emit(a) is True and f.should_emit(b) is False


def test_outside_window_not_suppressed():
    text = "la versione tre della suite va distribuita a tutti i pc"
    f = CrossTalkFilter(window_s=2.0)
    f.add(frame("A", text, 1000.0, 0.08)); f.add(frame("B", text, 1003.0, 0.01))
    assert f.should_emit(frame("B", text, 1003.0, 0.01)) is True


def test_different_text_not_suppressed():
    f = CrossTalkFilter()
    a = frame("A", "the drop matches a grid event not a hardware fault", 1000.0, 0.08)
    b = frame("B", "per il report mensile servono i grafici degli angoli", 1000.3, 0.01)
    f.add(a); f.add(b)
    assert f.should_emit(b) is True


def test_short_phrases_never_suppressed():
    f = CrossTalkFilter()
    a, b = frame("A", "ok va bene", 1000.0, 0.08), frame("B", "ok, va bene", 1000.1, 0.01)
    f.add(a); f.add(b)
    assert f.should_emit(a) is True and f.should_emit(b) is True


def test_same_speaker_never_suppressed():
    text = "mi occupo io della parte autocad entro la prossima settimana"
    f = CrossTalkFilter()
    f.add(frame("A", text, 1000.0, 0.08, start=0.0)); f.add(frame("A", text, 1000.5, 0.01, start=2.0))
    assert f.should_emit(frame("A", text, 1000.5, 0.01, start=2.0)) is True


def test_prune_forgets_old_frames():
    text = "gabriele puoi verificare i dati dell inverter entro venerdi"
    f = CrossTalkFilter(keep_s=20.0)
    f.add(frame("A", text, 1000.0, 0.08))
    f.prune(now=1000.0 + 25)
    assert f.should_emit(frame("B", text, 1000.5, 0.01)) is True
