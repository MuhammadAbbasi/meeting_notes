from pathlib import Path
import asyncio
import json

import numpy as np
import pytest

from server.meeting import MEETING_ID_RE, SPEAKER_RE, MeetingRoom, StreamClock, caption_frame
from server.transcriber import Segment


def pcm(samples, value=0.0):
    return np.full(samples, value, dtype=np.float32).tobytes()


def test_clock_counts_audio_time_and_rms():
    c = StreamClock()
    assert c.start_ts is None
    rms = c.add(pcm(16000, 0.5))
    assert c.start_ts is not None
    assert c.audio_time == 1.0
    assert rms == pytest.approx(0.5)
    assert c.rms_between(0, 1) == pytest.approx(0.5)
    assert c.rms_between(5, 6) == 0.0


def test_clock_rejects_misaligned_frame():
    c = StreamClock()
    with pytest.raises(ValueError):
        c.add(b"\x00\x00\x00")
    assert c.audio_time == 0.0


def test_caption_frame_fields():
    c = StreamClock()
    for _ in range(16):  # 4 s in 256 ms chunks, like the client
        c.add(pcm(4096, 0.1))
    c.start_ts = 1000.0
    f = caption_frame("daily", "Muhammad", Segment(2.0, 3.5, "hi", True), c)
    assert set(f) == {"type", "meeting_id", "speaker", "text", "completed", "start", "end", "ts", "rms"}
    assert f["type"] == "caption" and f["ts"] == 1002.0 and f["rms"] == pytest.approx(0.1)


def test_room_record_and_history(tmp_path):
    r = MeetingRoom("daily", tmp_path)
    cap = {"type": "caption", "meeting_id": "daily", "speaker": "A", "text": "x",
           "completed": True, "start": 0.0, "end": 1.0, "ts": 5.0, "rms": 0.2}
    r.record(cap)
    r.record({**cap, "text": "y"})
    hist = r.history()
    assert [h["text"] for h in hist] == ["x", "y"]
    assert set(hist[0]) == {"speaker", "text", "start", "end", "ts", "rms"}
    assert len((tmp_path / "daily.jsonl").read_text().splitlines()) == 2
    assert MeetingRoom("empty", tmp_path).history() == []


def test_room_roster_after_reconnect(tmp_path):
    r = MeetingRoom("daily", tmp_path)
    r.add_speaker("A", object())
    r.add_speaker("A", object())  # reconnect replaces, never duplicates
    assert r.roster() == {"type": "roster", "meeting_id": "daily", "speakers": ["A"]}


class FakeWS:
    def __init__(self, dead=False):
        self.dead, self.sent = dead, []

    async def send_json(self, msg):
        if self.dead:
            raise RuntimeError("closed")
        self.sent.append(msg)


def test_broadcast_drops_dead_socket(tmp_path):
    r = MeetingRoom("daily", tmp_path)
    good, dead, listener = FakeWS(), FakeWS(dead=True), FakeWS()
    r.add_speaker("A", good)
    r.add_speaker("B", dead)
    r.add_listener(listener)
    asyncio.run(r.broadcast({"type": "x"}))
    assert good.sent == [{"type": "x"}] and listener.sent == [{"type": "x"}]
    assert "B" not in r.speakers


def test_regexes():
    assert MEETING_ID_RE.fullmatch("daily_2026-11-23")
    assert not MEETING_ID_RE.fullmatch("../etc") and not MEETING_ID_RE.fullmatch("x y")
    assert SPEAKER_RE.fullmatch("Gian Domenico") and SPEAKER_RE.fullmatch("Remote-Call")
    assert not SPEAKER_RE.fullmatch("a/b") and not SPEAKER_RE.fullmatch("")


def test_broadcast_drops_stalled_and_broken_clients_without_delaying_others():
    import asyncio, time

    class Ok:
        def __init__(self): self.got = []
        async def send_json(self, m): self.got.append(m)

    class Stalled:
        async def send_json(self, m): await asyncio.sleep(60)

    class Broken:
        async def send_json(self, m): raise RuntimeError("socket gone")

    room = MeetingRoom("daily", Path("."))
    ok_l, ok_s, stalled, broken = Ok(), Ok(), Stalled(), Broken()
    room.listeners |= {ok_l, stalled, broken}
    room.add_speaker("Muhammad", ok_s)
    room.add_speaker("Gabriele", stalled)
    t0 = time.monotonic()
    asyncio.run(room.broadcast({"type": "x"}, timeout=0.2))
    assert time.monotonic() - t0 < 1.0
    assert ok_l.got == ok_s.got == [{"type": "x"}]
    assert room.listeners == {ok_l} and list(room.speakers) == ["Muhammad"]


def test_add_speaker_returns_replaced_and_remove_is_guarded():
    room = MeetingRoom("daily", Path("."))
    a, b = object(), object()
    assert room.add_speaker("Muhammad", a) is None
    assert room.add_speaker("Muhammad", b) is a
    room.remove_speaker("Muhammad", a)          # stale handler cleaning up late
    assert room.speakers["Muhammad"] is b
    room.remove_speaker("Muhammad", b)
    assert room.speakers == {}
