"""Meeting room state, per-stream audio clock and JSONL persistence. See CONTRACTS.md §1."""
from __future__ import annotations

import asyncio
import bisect
import json
import re
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

from server.deduplicator import CrossTalkFilter
from server.transcriber import Segment

MEETING_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
SPEAKER_RE = re.compile(r"[A-Za-z0-9 _.-]{1,40}")
CAPTION_FIELDS = ("speaker", "text", "start", "end", "ts", "rms")


class AudioRecorder:
    """Appends 16 kHz float32 mono PCM chunks to a 16-bit WAV file for replay/re-transcription."""

    def __init__(self, path: Path, sample_rate: int = 16000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(self.path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(sample_rate)

    def write(self, pcm_f32: bytes) -> None:
        samples = np.frombuffer(pcm_f32, dtype=np.float32)
        pcm_i16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
        self._wav.writeframes(pcm_i16.tobytes())

    def close(self) -> None:
        try:
            self._wav.close()
        except Exception:
            pass


class StreamClock:
    """Audio-time clock for one speaker stream plus a chunk-level RMS history."""

    def __init__(self, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate
        self.start_ts: float | None = None
        self._samples = 0
        self._rms: list[tuple[float, float, float]] = []  # (chunk start, chunk end, rms)
        self._t0: list[float] = []  # chunk starts, sorted, for bisect

    @property
    def audio_time(self) -> float:
        return self._samples / self.sample_rate

    def add(self, pcm: bytes) -> float:
        if len(pcm) % 4:
            raise ValueError(f"PCM frame length {len(pcm)} is not a multiple of 4")
        if self.start_ts is None:
            self.start_ts = time.time()
        chunk = np.frombuffer(pcm, dtype=np.float32)
        rms = float(np.sqrt(np.mean(chunk * chunk))) if chunk.size else 0.0
        t0 = self.audio_time
        self._samples += chunk.size
        self._rms.append((t0, self.audio_time, rms))
        self._t0.append(t0)
        return rms

    def peak_between(self, start: float, end: float) -> float:
        """Loudest chunk RMS overlapping [start, end) in this stream's audio time (bisect: cheap on 4 h streams)."""
        i = max(0, bisect.bisect_right(self._t0, start) - 1)
        j = bisect.bisect_left(self._t0, end)
        return max((r for _, t1, r in self._rms[i:j] if t1 > start), default=0.0)

    def rms_between(self, start: float, end: float) -> float:
        """Mean RMS of chunks overlapping [start, end)."""
        # ponytail: linear scan; ~56k chunks for a 4 h meeting, fine. Bisect if it ever shows up.
        vals = [r for t0, t1, r in self._rms if t0 < end and t1 > start]
        return float(sum(vals) / len(vals)) if vals else 0.0

    def abs_time(self, start: float) -> float:
        return (self.start_ts or 0.0) + start


def caption_frame(meeting_id: str, speaker: str, seg: Segment, clock: StreamClock) -> dict:
    return {
        "type": "caption",
        "meeting_id": meeting_id,
        "speaker": speaker,
        "text": seg.text,
        "completed": seg.completed,
        "start": seg.start,
        "end": seg.end,
        "ts": clock.abs_time(seg.start),
        "rms": clock.rms_between(seg.start, seg.end),
    }


class MeetingRoom:
    def __init__(self, meeting_id: str, data_dir: Path, dedup: CrossTalkFilter | None = None) -> None:
        self.meeting_id = meeting_id
        self.path = Path(data_dir) / f"{meeting_id}.jsonl"
        self.speakers: dict[str, Any] = {}
        self.listeners: set[Any] = set()
        self.clocks: dict[str, StreamClock] = {}  # per speaker, for the same-room echo check
        self.dedup = dedup or CrossTalkFilter()

    def add_speaker(self, name: str, ws: Any) -> Any:
        """Register `ws` for `name`; returns the connection it replaced (a reconnect after a network drop,
        or another PC using the same name), or None."""
        old = self.speakers.get(name)
        self.speakers[name] = ws
        return old

    def remove_speaker(self, name: str, ws: Any = None) -> None:
        """Remove `name`, but only if it is still `ws` (a replaced connection must not evict its successor)."""
        if ws is None or self.speakers.get(name) is ws:
            self.speakers.pop(name, None)

    def add_listener(self, ws: Any) -> None:
        self.listeners.add(ws)

    def remove_listener(self, ws: Any) -> None:
        self.listeners.discard(ws)

    def roster(self) -> dict:
        return {"type": "roster", "meeting_id": self.meeting_id, "speakers": list(self.speakers)}

    async def broadcast(self, msg: dict, timeout: float = 2.0) -> None:
        """Send to everyone at once; a client that fails or stalls longer than `timeout` is dropped,
        so one dead Wi-Fi laptop never delays captions for the rest of the room."""
        targets = [(name, ws) for name, ws in self.speakers.items()] + [(None, ws) for ws in self.listeners]
        if not targets:
            return
        results = await asyncio.gather(*(asyncio.wait_for(ws.send_json(msg), timeout) for _, ws in targets),
                                       return_exceptions=True)
        for (name, ws), res in zip(targets, results):
            if isinstance(res, BaseException):
                if name is not None:
                    if self.speakers.get(name) is ws:
                        self.speakers.pop(name, None)
                else:
                    self.listeners.discard(ws)

    def record(self, caption: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {k: caption[k] for k in CAPTION_FIELDS}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def history(self) -> list[dict]:
        """Persisted captions; a torn or corrupt line (crash mid-write, manual edit) is skipped, not fatal."""
        if not self.path.exists():
            return []
        rows = []
        with self.path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and "speaker" in row and "text" in row:
                    rows.append(row)
        return rows
