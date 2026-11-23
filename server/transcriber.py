"""Transcriber layer: WhisperLive adapter and a deterministic mock. See CONTRACTS.md §2–§3."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

import websockets

log = logging.getLogger(__name__)

END_OF_AUDIO = b"END_OF_AUDIO"
MOCK_BLOCK = 32000  # samples per completed segment (2.0 s at 16 kHz)
MOCK_PARTIAL = 16000  # samples before the partial inside a block (1.0 s)


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    completed: bool


OnSegments = Callable[[list[Segment]], Awaitable[None]]


class TranscriberUnavailable(Exception):
    pass


class SegmentTracker:
    """Turns WhisperLive's repeated last-N lists into once-only completed segments + the partial."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def ingest(self, segments: list[dict]) -> list[Segment]:
        out: list[Segment] = []
        for s in segments:
            text = (s.get("text") or "").strip()
            if s.get("completed"):
                if s["start"] in self._seen:
                    continue
                self._seen.add(s["start"])
            elif not text:
                continue
            out.append(Segment(float(s["start"]), float(s["end"]), text, bool(s.get("completed"))))
        return out


def build_handshake(uid: str, language: str | None, model: str) -> dict:
    return {
        "uid": uid,
        "language": language,
        "task": "transcribe",
        "model": model,
        "use_vad": True,
        "send_last_n_segments": 10,
        "no_speech_thresh": 0.45,
        "clip_audio": False,
        "same_output_threshold": 10,
        "audio_format": "float32",
    }


class Transcriber(Protocol):
    kind: str

    async def start(self, on_segments: OnSegments) -> None: ...
    async def feed(self, pcm: bytes) -> None: ...
    async def end(self) -> None: ...
    async def close(self) -> None: ...


class MockTranscriber:
    kind = "mock"

    def __init__(self, speaker: str, script: list[str] | None = None) -> None:
        self.speaker = speaker
        self.script = list(script or [])
        self._on: OnSegments | None = None
        self._samples = 0  # total samples received
        self._block_start = 0  # sample index where the current block began
        self._n = 0  # completed segments emitted
        self._partial_sent = False

    def _line(self, n: int) -> str:
        return self.script[n - 1] if n - 1 < len(self.script) else f"{self.speaker} segment {n}"

    async def start(self, on_segments: OnSegments) -> None:
        self._on = on_segments

    async def feed(self, pcm: bytes) -> None:
        assert self._on is not None
        self._samples += len(pcm) // 4
        while True:
            in_block = self._samples - self._block_start
            t0 = self._block_start / 16000
            if in_block >= MOCK_BLOCK:
                text = self._line(self._n + 1)
                self._n += 1
                self._block_start += MOCK_BLOCK
                self._partial_sent = False
                await self._on([Segment(t0, t0 + 2.0, text, True)])
                continue
            if in_block >= MOCK_PARTIAL and not self._partial_sent:
                self._partial_sent = True
                text = self._line(self._n + 1)
                await self._on([Segment(t0, t0 + 1.0, text[: len(text) // 2], False)])
            return

    async def end(self) -> None:
        pass

    async def close(self) -> None:
        pass


class WhisperLiveTranscriber:
    kind = "whisperlive"

    def __init__(self, url: str, uid: str, language: str | None, model: str) -> None:
        self.url, self.uid, self.language, self.model = url, uid, language, model
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._tracker = SegmentTracker()

    async def start(self, on_segments: OnSegments) -> None:
        try:
            self._ws = await websockets.connect(self.url, max_size=None)
            await self._ws.send(json.dumps(build_handshake(self.uid, self.language, self.model)))
            while True:
                msg = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=60))
                if msg.get("status") == "WAIT":
                    raise TranscriberUnavailable(f"WhisperLive full, wait ~{msg.get('message')} min")
                if msg.get("message") == "SERVER_READY":
                    break
        except TranscriberUnavailable:
            await self.close()
            raise
        except Exception as e:  # connect refused, timeout, bad JSON
            await self.close()
            raise TranscriberUnavailable(f"WhisperLive unreachable: {e}") from e
        self._reader = asyncio.create_task(self._read(on_segments))

    async def _read(self, on_segments: OnSegments) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if "segments" in msg:
                    segs = self._tracker.ingest(msg["segments"])
                    if segs:
                        await on_segments(segs)
                elif msg.get("message") == "DISCONNECT":
                    log.info("%s: WhisperLive disconnected", self.uid)
                    return
        except websockets.ConnectionClosed:
            pass

    async def feed(self, pcm: bytes) -> None:
        if self._ws is not None:
            await self._ws.send(pcm)

    async def end(self) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(END_OF_AUDIO)
            if self._reader:
                await asyncio.wait_for(asyncio.shield(self._reader), timeout=2.0)
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            pass

    async def close(self) -> None:
        if self._reader and not self._reader.done():
            self._reader.cancel()
        if self._ws is not None:
            await self._ws.close()
            self._ws = None


def _load_script(speaker: str) -> list[str] | None:
    path = os.getenv("MOCK_SCRIPT")
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f).get(speaker)


def make_transcriber(meeting_id: str, speaker: str, language: str) -> Transcriber:
    lang = None if language == "auto" else language
    if os.getenv("TRANSCRIBER", "whisperlive") == "mock":
        return MockTranscriber(speaker, _load_script(speaker))
    return WhisperLiveTranscriber(
        url=os.getenv("WHISPERLIVE_URL", "ws://whisperlive:9090"),
        uid=f"{meeting_id}:{speaker}:{uuid.uuid4().hex[:8]}",
        language=lang,
        model=os.getenv("WHISPER_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2"),
    )
