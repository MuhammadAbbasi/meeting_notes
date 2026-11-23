"""Caption hub: proxies workstation audio to the transcriber and fans captions out per meeting.

Wire protocol: CONTRACTS.md §1. Run: uvicorn server.hub:app --host 0.0.0.0 --port 8085
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState

from server.deduplicator import CrossTalkFilter
from server.meeting import MEETING_ID_RE, SPEAKER_RE, MeetingRoom, StreamClock, caption_frame
from server.odoo_client import OdooMeetingClient
from server.ollama_synthesizer import SynthesisUnavailable, synthesize, transcript_markdown
from server.transcriber import TranscriberUnavailable, make_transcriber

log = logging.getLogger("hub")
DATA_DIR = Path(os.getenv("DATA_DIR", "./data/meetings"))
TRANSCRIBER_KIND = os.getenv("TRANSCRIBER", "whisperlive")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
DEDUP_HOLD_S = float(os.getenv("DEDUP_HOLD_S", "2.5"))
DEDUP_WINDOW_S = float(os.getenv("DEDUP_WINDOW_S", "2.0"))
DEDUP_SIMILARITY = float(os.getenv("DEDUP_SIMILARITY", "0.78"))
VIEWER_HTML = (Path(__file__).parent / "static" / "captions.html").read_text(encoding="utf-8")

app = FastAPI(title="caption-hub")
rooms: dict[str, MeetingRoom] = {}
_pending: set[asyncio.Task] = set()  # keeps hold-window tasks alive


def get_room(meeting_id: str) -> MeetingRoom:
    if meeting_id not in rooms:
        rooms[meeting_id] = MeetingRoom(meeting_id, DATA_DIR, CrossTalkFilter(DEDUP_WINDOW_S, DEDUP_SIMILARITY))
    return rooms[meeting_id]


async def _settle(room: MeetingRoom, frame: dict) -> None:
    """After the hold window, emit the completed caption or retract it as mic bleed."""
    if DEDUP_HOLD_S > 0:
        await asyncio.sleep(DEDUP_HOLD_S)
    if room.dedup.should_emit(frame):
        room.record(frame)
        await room.broadcast(frame)
    else:
        log.info("%s: suppressed bleed from %s: %r", room.meeting_id, frame["speaker"], frame["text"][:40])
        await room.broadcast({"type": "retract", "meeting_id": room.meeting_id, "speaker": frame["speaker"]})


def _drop_if_empty(room: MeetingRoom) -> None:
    if not room.speakers and not room.listeners:
        rooms.pop(room.meeting_id, None)


def _valid_id(meeting_id: str) -> str:
    if not MEETING_ID_RE.fullmatch(meeting_id):
        raise HTTPException(400, "invalid meeting_id")
    return meeting_id


async def _close(ws: WebSocket, code: int = 1000) -> None:
    if ws.client_state != WebSocketState.DISCONNECTED:
        try:
            await ws.close(code=code)
        except RuntimeError:
            pass


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "transcriber": TRANSCRIBER_KIND, "rooms": len(rooms)}


@app.get("/captions/{meeting_id}", response_class=HTMLResponse)
async def viewer(meeting_id: str) -> str:
    return VIEWER_HTML.replace("__MEETING_ID__", _valid_id(meeting_id))


@app.get("/api/meetings/{meeting_id}/segments")
async def segments(meeting_id: str) -> list[dict]:
    return MeetingRoom(_valid_id(meeting_id), DATA_DIR).history()


@app.post("/api/meetings/{meeting_id}/finalize")
async def finalize(meeting_id: str) -> dict:
    """Consolidate the meeting into Markdown minutes (Ollama, with transcript fallback) and publish."""
    _valid_id(meeting_id)
    segs = MeetingRoom(meeting_id, DATA_DIR).history()
    if not segs:
        raise HTTPException(404, "no completed captions for this meeting")
    try:
        markdown = await asyncio.to_thread(synthesize, segs, meeting_id, OLLAMA_URL, OLLAMA_MODEL)
        llm = True
    except SynthesisUnavailable as e:
        log.warning("%s: %s; falling back to transcript", meeting_id, e)
        markdown, llm = transcript_markdown(segs), False
    participants = sorted({s["speaker"] for s in segs})
    odoo = await asyncio.to_thread(OdooMeetingClient().publish_or_save, meeting_id, markdown, participants, DATA_DIR)
    if meeting_id in rooms:
        await rooms[meeting_id].broadcast({"type": "finalized", "meeting_id": meeting_id, "llm": llm,
                                           "odoo_status": odoo["status"]})
    return {"meeting_id": meeting_id, "markdown": markdown, "llm": llm, "saved_to": odoo["path"], "odoo": odoo}


@app.websocket("/listen/{meeting_id}")
async def listen(ws: WebSocket, meeting_id: str) -> None:
    await ws.accept()
    if not MEETING_ID_RE.fullmatch(meeting_id):
        await _close(ws, 1008)
        return
    room = get_room(meeting_id)
    room.add_listener(ws)
    try:
        await ws.send_json(room.roster())
        while True:
            await ws.receive()  # listeners send nothing; this just waits for disconnect
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        room.remove_listener(ws)
        _drop_if_empty(room)


@app.websocket("/ws/{meeting_id}/{speaker}")
async def speak(ws: WebSocket, meeting_id: str, speaker: str, lang: str = "auto") -> None:
    await ws.accept()
    if not MEETING_ID_RE.fullmatch(meeting_id) or not SPEAKER_RE.fullmatch(speaker):
        await _close(ws, 1008)
        return
    room = get_room(meeting_id)
    if not room.add_speaker(speaker, ws):
        await ws.send_json({"type": "error", "message": f"speaker '{speaker}' already connected"})
        await _close(ws, 1008)
        _drop_if_empty(room)
        return

    clock = StreamClock()

    async def on_segments(segs) -> None:
        for seg in segs:
            frame = caption_frame(meeting_id, speaker, seg, clock)
            if seg.completed:
                room.dedup.add(frame)
                task = asyncio.create_task(_settle(room, frame))
                _pending.add(task)
                task.add_done_callback(_pending.discard)
            else:
                await room.broadcast(frame)

    transcriber = None
    try:
        try:
            transcriber = make_transcriber(meeting_id, speaker, lang)
            await transcriber.start(on_segments)
        except TranscriberUnavailable as e:
            await ws.send_json({"type": "error", "message": str(e)})
            return
        await ws.send_json({"type": "ready", "meeting_id": meeting_id, "speaker": speaker,
                            "transcriber": transcriber.kind})
        await room.broadcast(room.roster())

        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                try:
                    clock.add(msg["bytes"])
                except ValueError as e:
                    log.warning("%s/%s: dropped frame: %s", meeting_id, speaker, e)
                    continue
                await transcriber.feed(msg["bytes"])
            elif msg.get("text"):
                try:
                    cmd = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                if cmd.get("type") == "end":
                    await transcriber.end()
                    break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if transcriber is not None:
            await transcriber.close()
        room.remove_speaker(speaker)
        await _close(ws)
        await room.broadcast(room.roster())
        _drop_if_empty(room)
