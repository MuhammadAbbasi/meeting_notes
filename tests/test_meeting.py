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


def test_room_duplicate_speaker(tmp_path):
    r = MeetingRoom("daily", tmp_path)
    assert r.add_speaker("A", object()) is True
    assert r.add_speaker("A", object()) is False
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
