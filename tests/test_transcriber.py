import asyncio

import numpy as np
import pytest

from server.transcriber import (
    MockTranscriber,
    Segment,
    SegmentTracker,
    build_handshake,
    make_transcriber,
)


def seg(start, end, text, completed):
    return {"start": f"{start:.3f}", "end": f"{end:.3f}", "text": text, "completed": completed}


def test_tracker_emits_completed_once():
    t = SegmentTracker()
    msg = [seg(0, 1, " a ", True)]
    assert t.ingest(msg) == [Segment(0.0, 1.0, "a", True)]
    assert t.ingest(msg) == []


def test_tracker_partial_follows_completed():
    t = SegmentTracker()
    a = seg(0, 1, "a", True)
    assert t.ingest([a, seg(1, 1.5, "b", False)]) == [
        Segment(0.0, 1.0, "a", True),
        Segment(1.0, 1.5, "b", False),
    ]
    assert t.ingest([a, seg(1, 1.8, "b2", False)]) == [Segment(1.0, 1.8, "b2", False)]


def test_tracker_skips_empty_partial():
    assert SegmentTracker().ingest([seg(0, 0.5, "  ", False)]) == []


def test_build_handshake_matches_contract():
    assert build_handshake("m:s:abcd1234", None, "x/y") == {
        "uid": "m:s:abcd1234",
        "language": None,
        "task": "transcribe",
        "model": "x/y",
        "use_vad": True,
        "send_last_n_segments": 10,
        "no_speech_thresh": 0.45,
        "clip_audio": False,
        "same_output_threshold": 10,
        "audio_format": "float32",
    }


def silence(samples):
    return np.zeros(samples, dtype=np.float32).tobytes()


def run_mock(script=None, feeds=(16000, 16000)):
    got = []

    async def on_segments(segs):
        got.append(list(segs))

    async def main():
        m = MockTranscriber("Muhammad", script)
        await m.start(on_segments)
        for n in feeds:
            await m.feed(silence(n))
        await m.close()

    asyncio.run(main())
    return got


def test_mock_emits_partial_then_completed():
    got = run_mock()
    assert got[0] == [Segment(0.0, 1.0, "Muhammad ", False)]  # first 9 of 18 chars
    assert got[1] == [Segment(0.0, 2.0, "Muhammad segment 1", True)]


def test_mock_uses_script():
    got = run_mock(["ciao", "come va"], feeds=(32000, 32000, 32000))
    completed = [s for batch in got for s in batch if s.completed]
    assert [s.text for s in completed] == ["ciao", "come va", "Muhammad segment 3"]
    assert completed[1] == Segment(2.0, 4.0, "come va", True)


def test_make_transcriber_mock_from_env(monkeypatch):
    monkeypatch.setenv("TRANSCRIBER", "mock")
    monkeypatch.delenv("MOCK_SCRIPT", raising=False)
    assert make_transcriber("daily", "Muhammad", "auto").kind == "mock"


def test_make_transcriber_whisperlive_default(monkeypatch):
    monkeypatch.delenv("TRANSCRIBER", raising=False)
    t = make_transcriber("daily", "Gabriele", "it")
    assert t.kind == "whisperlive"
    assert t.uid.startswith("daily:Gabriele:") and len(t.uid.split(":")[-1]) == 8
    assert t.language == "it"
    assert make_transcriber("daily", "Gabriele", "auto").language is None
