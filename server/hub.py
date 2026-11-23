"""Caption hub: proxies workstation audio to the transcriber and fans captions out per meeting.

Wire protocol: CONTRACTS.md §1. Run: uvicorn server.hub:app --host 0.0.0.0 --port 8085
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState

from server.deduplicator import CrossTalkFilter
from server.meeting import MEETING_ID_RE, SPEAKER_RE, MeetingRoom, StreamClock, caption_frame
from server.odoo_client import OdooMeetingClient
from server.ollama_synthesizer import SynthesisUnavailable, synthesize, transcript_markdown
from server.transcriber import TranscriberUnavailable, make_transcriber

log = logging.getLogger("hub")
DATA_DIR = Path(os.getenv("DATA_DIR", "./data/meetings"))
TRANSCRIBER_KIND = os.getenv("TRANSCRIBER", "whisperlive")
WHISPERLIVE_URL = os.getenv("WHISPERLIVE_URL", "ws://whisperlive:9090")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
DEDUP_HOLD_S = float(os.getenv("DEDUP_HOLD_S", "2.5"))
DEDUP_WINDOW_S = float(os.getenv("DEDUP_WINDOW_S", "2.0"))
DEDUP_SIMILARITY = float(os.getenv("DEDUP_SIMILARITY", "0.78"))
_STATIC = Path(__file__).parent / "static"
VIEWER_HTML = (_STATIC / "captions.html").read_text(encoding="utf-8")
DASHBOARD_HTML = (_STATIC / "dashboard.html").read_text(encoding="utf-8")

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


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    return DASHBOARD_HTML


async def _reachable(url: str, timeout: float = 2.0) -> bool:
    """TCP connect check; enough to tell 'container down' from 'up' without speaking the protocol."""
    u = urlsplit(url)
    port = u.port or (443 if u.scheme in ("https", "wss") else 80)
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(u.hostname, port), timeout)
        w.close()
        return True
    except (OSError, asyncio.TimeoutError, ValueError):
        return False


def _ollama_models(url: str, timeout: float = 2.0) -> list[str] | None:
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=timeout) as r:
            return sorted(m["name"] for m in json.load(r).get("models", []))
    except Exception:
        return None


@app.get("/api/settings")
async def settings() -> dict:
    """Effective configuration plus live reachability of each dependency. Secrets are masked."""
    odoo = OdooMeetingClient()
    whisper_ok, odoo_ok, models = await asyncio.gather(
        _reachable(WHISPERLIVE_URL) if TRANSCRIBER_KIND != "mock" else asyncio.sleep(0, True),
        _reachable(odoo.url) if odoo.url else asyncio.sleep(0, False),
        asyncio.to_thread(_ollama_models, OLLAMA_URL))
    pulled = any(m == OLLAMA_MODEL or m.split(":")[0] == OLLAMA_MODEL for m in models or [])
    return {
        "transcriber": {"kind": TRANSCRIBER_KIND, "url": WHISPERLIVE_URL, "model": WHISPER_MODEL, "reachable": whisper_ok},
        "llm": {"url": OLLAMA_URL, "model": OLLAMA_MODEL, "reachable": models is not None,
                "model_pulled": pulled, "models": models or []},
        "odoo": {"url": odoo.url, "db": odoo.db, "user": odoo.username, "api_key": "set" if odoo.api_key else "not set",
                 "project_id": odoo.project_id, "configured": odoo.is_configured, "reachable": odoo_ok,
                 "speaker_aliases": odoo.speaker_aliases},
        "dedup": {"hold_s": DEDUP_HOLD_S, "window_s": DEDUP_WINDOW_S, "similarity": DEDUP_SIMILARITY},
        "storage": {"data_dir": str(DATA_DIR.resolve()), "meetings": len(list(DATA_DIR.glob("*.jsonl")))},
        "live_rooms": {mid: sorted(r.speakers) for mid, r in rooms.items()},
    }


def _meeting_summary(path: Path) -> dict:
    mid = path.stem
    segs = MeetingRoom(mid, DATA_DIR).history()  # ponytail: re-reads every jsonl per request; fine for an office
    ts = [s["ts"] for s in segs]
    info = DATA_DIR / f"{mid}.finalize.json"
    if info.exists():
        minutes = json.loads(info.read_text(encoding="utf-8"))
    else:
        minutes = {"status": "file_only"} if (DATA_DIR / f"{mid}.minutes.md").exists() else None
    return {"meeting_id": mid, "segments": len(segs), "speakers": sorted({s["speaker"] for s in segs}),
            "started": min(ts, default=None), "ended": max(ts, default=None), "live": mid in rooms, "minutes": minutes}


@app.get("/api/meetings")
async def meetings() -> list[dict]:
    rows = [_meeting_summary(p) for p in DATA_DIR.glob("*.jsonl")]
    return sorted(rows, key=lambda r: r["ended"] or 0, reverse=True)


@app.get("/api/meetings/{meeting_id}/segments")
async def segments(meeting_id: str) -> list[dict]:
    return MeetingRoom(_valid_id(meeting_id), DATA_DIR).history()


@app.get("/api/meetings/{meeting_id}/minutes", response_class=PlainTextResponse)
async def minutes(meeting_id: str) -> str:
    path = DATA_DIR / f"{_valid_id(meeting_id)}.minutes.md"
    if not path.exists():
        raise HTTPException(404, "meeting not finalized yet")
    return path.read_text(encoding="utf-8")


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
    (DATA_DIR / f"{meeting_id}.finalize.json").write_text(
        json.dumps({"finalized_at": time.time(), "llm": llm, "odoo": odoo}), encoding="utf-8")
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
