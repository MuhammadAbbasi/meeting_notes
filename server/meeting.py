"""Meeting room state, per-stream audio clock and JSONL persistence. See CONTRACTS.md §1."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import numpy as np

from server.deduplicator import CrossTalkFilter
from server.transcriber import Segment

MEETING_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
SPEAKER_RE = re.compile(r"[A-Za-z0-9 _.-]{1,40}")
CAPTION_FIELDS = ("speaker", "text", "start", "end", "ts", "rms")


class StreamClock:
    """Audio-time clock for one speaker stream plus a chunk-level RMS history."""

    def __init__(self, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate
        self.start_ts: float | None = None
        self._samples = 0
        self._rms: list[tuple[float, float, float]] = []  # (chunk start, chunk end, rms)

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
        return rms

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
        self.dedup = dedup or CrossTalkFilter()

    def add_speaker(self, name: str, ws: Any) -> bool:
        if name in self.speakers:
            return False
        self.speakers[name] = ws
        return True

    def remove_speaker(self, name: str) -> None:
        self.speakers.pop(name, None)

    def add_listener(self, ws: Any) -> None:
        self.listeners.add(ws)

    def remove_listener(self, ws: Any) -> None:
        self.listeners.discard(ws)

    def roster(self) -> dict:
        return {"type": "roster", "meeting_id": self.meeting_id, "speakers": list(self.speakers)}

    async def broadcast(self, msg: dict) -> None:
        for name, ws in list(self.speakers.items()):
            try:
                await ws.send_json(msg)
            except Exception:
                self.speakers.pop(name, None)
        for ws in list(self.listeners):
            try:
                await ws.send_json(msg)
            except Exception:
                self.listeners.discard(ws)

    def record(self, caption: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {k: caption[k] for k in CAPTION_FIELDS}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def history(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
