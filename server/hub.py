"""Caption hub: proxies workstation audio to the transcriber and fans captions out per meeting.

Wire protocol: CONTRACTS.md §1. Run: uvicorn server.hub:app --host 0.0.0.0 --port 8085
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.websockets import WebSocketDisconnect, WebSocketState

from collections import deque

from server.deduplicator import CrossTalkFilter, is_room_noise, normalize_text
from server.docker_api import restart_container
from server.meeting import MEETING_ID_RE, SPEAKER_RE, MeetingRoom, StreamClock, caption_frame
from server.odoo_client import OdooMeetingClient
from server.ollama_synthesizer import SynthesisUnavailable, synthesize, transcript_markdown
from server.settings import FIELDS, Settings
from server.transcriber import TranscriberUnavailable, make_transcriber

log = logging.getLogger("hub")
DATA_DIR = Path(os.getenv("DATA_DIR", "./data/meetings"))
DIST_DIR = Path(os.getenv("DIST_DIR", "./data/dist"))
SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", "./data/settings.json"))
WHISPER_MODEL_FILE = Path(os.getenv("WHISPER_MODEL_FILE", "./data/config/whisper_model"))
WHISPERLIVE_CONTAINER = os.getenv("WHISPERLIVE_CONTAINER", "caption-whisperlive")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
EXE_NAME = "OfficeMeetingAgent.exe"
HUB_VERSION = "2026.11.25-5"  # bump on every deploy-relevant change; /health reports it
_STATIC = Path(__file__).parent / "static"
VIEWER_HTML = (_STATIC / "captions.html").read_text(encoding="utf-8")
DASHBOARD_HTML = (_STATIC / "dashboard.html").read_text(encoding="utf-8")

app = FastAPI(title="caption-hub")
S = Settings(SETTINGS_FILE)
rooms: dict[str, MeetingRoom] = {}
_pending: set[asyncio.Task] = set()  # keeps hold-window tasks alive
_finalizing: dict[str, asyncio.Lock] = {}  # one minutes run per meeting, however many people press the button
_summary_cache: dict[str, tuple[tuple, dict]] = {}  # meeting_id -> (file stats, summary)
_basic = HTTPBasic(auto_error=False)  # no WWW-Authenticate: the page has its own login form, browsers never prompt


def admin(cred: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    """HTTP Basic credentials on every admin API call; any username, ADMIN_PASSWORD as password."""
    if not ADMIN_PASSWORD:
        raise HTTPException(403, "admin disabled: set ADMIN_PASSWORD on the hub")
    if cred is None:
        raise HTTPException(401, "login required")
    if not secrets.compare_digest(cred.password.encode(), ADMIN_PASSWORD.encode()):
        raise HTTPException(401, "wrong password")


def odoo_client(**overrides) -> OdooMeetingClient:
    v = {k: S.get(k) for k in ("odoo_url", "odoo_db", "odoo_user", "odoo_api_key", "odoo_project_id", "speaker_aliases")}
    v.update({k: x for k, x in overrides.items() if k in v and x not in (None, "")})
    return OdooMeetingClient(url=v["odoo_url"], db=v["odoo_db"], username=v["odoo_user"], api_key=v["odoo_api_key"],
                             project_id=v["odoo_project_id"], speaker_aliases=v["speaker_aliases"])


def get_room(meeting_id: str) -> MeetingRoom:
    if meeting_id not in rooms:
        rooms[meeting_id] = MeetingRoom(meeting_id, DATA_DIR,
                                        CrossTalkFilter(S.get("dedup_window_s"), S.get("dedup_similarity")))
    return rooms[meeting_id]


async def _settle(room: MeetingRoom, frame: dict) -> None:
    """After the hold window, emit the completed caption or retract it as mic bleed."""
    hold = S.get("dedup_hold_s")
    if hold > 0:
        await asyncio.sleep(hold)
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


# --- status & admin ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/dashboard")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": HUB_VERSION, "transcriber": S.get("transcriber"), "rooms": len(rooms),
            "speakers": sum(len(r.speakers) for r in rooms.values()),
            "listeners": sum(len(r.listeners) for r in rooms.values())}


NO_CACHE = {"Cache-Control": "no-cache"}  # browsers revalidate, so a redeploy reaches every open screen


@app.get("/captions/{meeting_id}", response_class=HTMLResponse)
async def viewer(meeting_id: str) -> HTMLResponse:
    return HTMLResponse(VIEWER_HTML.replace("__MEETING_ID__", _valid_id(meeting_id)), headers=NO_CACHE)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML, headers=NO_CACHE)


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


_sha_cache: dict = {}


def _agent_info() -> dict | None:
    exe = DIST_DIR / EXE_NAME
    if not exe.exists():
        return None
    st = exe.stat()
    key = (st.st_size, st.st_mtime_ns)
    if _sha_cache.get("key") != key:  # hash once per uploaded build, not on every poll
        h = hashlib.sha256()
        with exe.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _sha_cache.update(key=key, sha256=h.hexdigest().upper())
    return {"name": EXE_NAME, "size": st.st_size, "modified": st.st_mtime, "sha256": _sha_cache["sha256"]}


@app.get("/api/settings", dependencies=[Depends(admin)])
async def settings() -> dict:
    """Editable values (secrets masked) plus live reachability of each dependency."""
    odoo = odoo_client()
    whisper_ok, odoo_ok, models = await asyncio.gather(
        _reachable(S.get("whisperlive_url")) if S.get("transcriber") != "mock" else asyncio.sleep(0, True),
        _reachable(odoo.url) if odoo.url else asyncio.sleep(0, False),
        asyncio.to_thread(_ollama_models, S.get("ollama_url")))
    model = S.get("ollama_model")
    return {
        "values": S.public(),
        "version": S.version,
        "status": {
            "transcriber_reachable": whisper_ok,
            "whisperlive_container": WHISPERLIVE_CONTAINER,
            "llm_reachable": models is not None,
            "llm_models": models or [],
            "llm_model_pulled": any(m == model or m.split(":")[0] == model for m in models or []),
            "odoo_configured": odoo.is_configured,
            "odoo_reachable": odoo_ok,
            "data_dir": str(DATA_DIR.resolve()),
            "meetings": sum(1 for p in DATA_DIR.glob("*.jsonl") if MEETING_ID_RE.fullmatch(p.stem)),
            "live_rooms": {mid: sorted(r.speakers) for mid, r in rooms.items()},
            "agent": _agent_info(),
        },
    }


def _apply_whisper_model(model: str) -> str:
    """Write the model for the WhisperLive container and restart it; the compose command reads that file."""
    WHISPER_MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    WHISPER_MODEL_FILE.write_text(model, encoding="utf-8")
    try:
        restart_container(WHISPERLIVE_CONTAINER)
        return "whisperlive restarting (first use of a new model downloads it: minutes)"
    except Exception as e:  # no docker socket (dev) or container missing
        return f"model saved; restart WhisperLive manually ({type(e).__name__}: {e})"


@app.put("/api/settings", dependencies=[Depends(admin)])
async def update_settings(patch: dict) -> dict:
    base = patch.pop("_version", None)
    if base is not None and base != S.version:
        raise HTTPException(409, "settings were changed by someone else since you opened them")
    try:
        changed = S.update(patch)
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        raise HTTPException(400, f"invalid value: {e}")
    notes = []
    if "whisper_model" in changed and S.get("transcriber") != "mock":
        notes.append(await asyncio.to_thread(_apply_whisper_model, S.get("whisper_model")))
    if "transcriber" in changed or "whisperlive_url" in changed:
        notes.append("new connections use the new transcriber; running streams keep the old one")
    log.info("settings changed: %s", changed)
    return {"changed": changed, "values": S.public(), "version": S.version, "notes": notes}


@app.post("/api/settings/test", dependencies=[Depends(admin)])
async def test_settings(body: dict) -> dict:
    """Try the posted (unsaved) values: what = odoo | ollama | whisperlive."""
    what = body.get("what")
    v = {k: body.get(k) for k in FIELDS}
    if what == "odoo":
        client = odoo_client(**v)
        if not client.is_configured:
            return {"ok": False, "message": "URL, database, user and API key are all required"}
        try:
            users = await asyncio.to_thread(client.fetch_active_users)
        except Exception as e:
            return {"ok": False, "message": f"{type(e).__name__}: {e}"}
        names = sorted(u["name"] for u in users)
        return {"ok": True, "message": f"login ok, {len(users)} internal users: {', '.join(names[:8])}"
                                       + (" …" if len(names) > 8 else ""), "users": names}
    if what == "ollama":
        url = (v.get("ollama_url") or S.get("ollama_url")).rstrip("/")
        model = v.get("ollama_model") or S.get("ollama_model")
        models = await asyncio.to_thread(_ollama_models, url)
        if models is None:
            return {"ok": False, "message": f"no Ollama at {url}"}
        pulled = any(m == model or m.split(":")[0] == model for m in models)
        return {"ok": pulled, "models": models,
                "message": f"reachable, model {'present' if pulled else 'NOT pulled: run ollama pull ' + model}"}
    if what == "whisperlive":
        url = v.get("whisperlive_url") or S.get("whisperlive_url")
        ok = await _reachable(url)
        return {"ok": ok, "message": f"{url} {'answers' if ok else 'does not answer'}"}
    raise HTTPException(400, "what must be odoo, ollama or whisperlive")


@app.post("/api/whisperlive/restart", dependencies=[Depends(admin)])
async def whisperlive_restart() -> dict:
    return {"message": await asyncio.to_thread(_apply_whisper_model, S.get("whisper_model"))}


# --- workstation agent distribution ----------------------------------------------------------

@app.put("/api/agent/exe", dependencies=[Depends(admin)])
async def upload_exe(request: Request) -> dict:
    """Raw body = the built OfficeMeetingAgent.exe (no multipart, so no extra dependency)."""
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIST_DIR / (EXE_NAME + ".part")
    size = 0
    with tmp.open("wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
            size += len(chunk)
    if size < 1024:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, "empty upload")
    tmp.replace(DIST_DIR / EXE_NAME)
    return {"agent": _agent_info()}


@app.get("/download/" + EXE_NAME)
async def download_exe() -> FileResponse:
    exe = DIST_DIR / EXE_NAME
    if not exe.exists():
        raise HTTPException(404, "no agent uploaded yet: build it with client/build_exe.bat and upload it from the dashboard")
    return FileResponse(exe, media_type="application/octet-stream", filename=EXE_NAME)


INSTALL_PS1 = """# Meeting Captions installer, served by the hub at __BASE__
# Installs for the current Windows user only (no administrator rights needed).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$base = '__BASE__'
$expected = '__SHA256__'
if (-not $expected) { Write-Host 'The agent has not been uploaded to the hub yet. Ask the office admin.' -ForegroundColor Red; return }
$dir = Join-Path $env:LOCALAPPDATA 'MeetingCaptions'
New-Item -ItemType Directory -Force $dir | Out-Null
$exe = Join-Path $dir 'OfficeMeetingAgent.exe'
Write-Host 'Meeting Captions: downloading...' -ForegroundColor Cyan
Get-Process OfficeMeetingAgent -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500
Invoke-WebRequest "$base/download/OfficeMeetingAgent.exe" -OutFile "$exe.part" -UseBasicParsing
$hash = (Get-FileHash "$exe.part" -Algorithm SHA256).Hash
if ($hash -ne $expected) {
  Remove-Item "$exe.part" -Force
  Write-Host 'The download does not match the hub''s fingerprint (damaged or changed on the way). Nothing was installed.' -ForegroundColor Red
  return
}
Move-Item "$exe.part" $exe -Force
$cfg = Join-Path $dir 'config.json'
if (-not (Test-Path $cfg)) { Invoke-WebRequest "$base/download/config.json" -OutFile $cfg -UseBasicParsing }
$ws = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Startup'))) {
  $lnk = $ws.CreateShortcut((Join-Path $folder 'Meeting Captions.lnk'))
  $lnk.TargetPath = $exe; $lnk.WorkingDirectory = $dir; $lnk.IconLocation = "$exe,0"
  $lnk.Description = 'Live meeting captions and minutes'; $lnk.Save()
}
Start-Process $exe -WorkingDirectory $dir
Write-Host 'Done. Meeting Captions is in the Start menu, starts with Windows, and is opening now.' -ForegroundColor Green
"""
UNINSTALL_PS1 = """# Meeting Captions: remove from this computer (current user)
$ErrorActionPreference = 'SilentlyContinue'
Get-Process OfficeMeetingAgent | Stop-Process -Force
Start-Sleep -Milliseconds 500
foreach ($folder in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Startup'))) {
  Remove-Item (Join-Path $folder 'Meeting Captions.lnk') -Force
}
Remove-Item (Join-Path $env:LOCALAPPDATA 'MeetingCaptions') -Recurse -Force
Write-Host 'Meeting Captions was removed from this computer.' -ForegroundColor Green
"""


def _base_url(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


@app.get("/install.ps1", response_class=PlainTextResponse)
async def install_script(request: Request) -> str:
    """One-command install for a new PC: irm http://HUB:8085/install.ps1 | iex
    Files fetched by PowerShell carry no browser download mark, so SmartScreen does not interrupt;
    the script refuses a download whose SHA-256 differs from the hub's copy."""
    agent = _agent_info()
    return INSTALL_PS1.replace("__BASE__", _base_url(request)).replace("__SHA256__", agent["sha256"] if agent else "")


@app.get("/uninstall.ps1", response_class=PlainTextResponse)
async def uninstall_script() -> str:
    return UNINSTALL_PS1


@app.get("/icon.png", include_in_schema=False)
async def icon() -> FileResponse:
    return FileResponse(_STATIC / "icon.png", media_type="image/png")


@app.get("/download/config.json")
async def download_config(request: Request) -> dict:
    """Pre-filled agent config: the setup dialog on a new PC only asks for the person's name."""
    return {"user_name": "", "server_ip": request.url.hostname, "hub_port": request.url.port or 80,
            "meeting_id": S.get("default_meeting"), "language": S.get("default_language"),
            "input_device": None, "overlay_alpha": 0.5, "capture_system_audio": False}


# --- meetings -----------------------------------------------------------------------------------

def _stat(p: Path) -> tuple | None:
    try:
        st = p.stat()
        return st.st_mtime_ns, st.st_size
    except FileNotFoundError:
        return None


def _meeting_summary(path: Path) -> dict:
    """Summary of one meeting, re-read only when one of its files changed (every admin polls this)."""
    mid = path.stem
    info, md = DATA_DIR / f"{mid}.finalize.json", DATA_DIR / f"{mid}.minutes.md"
    key = (_stat(path), _stat(info), _stat(md))
    hit = _summary_cache.get(mid)
    if hit and hit[0] == key:
        summary = hit[1]
    else:
        segs = MeetingRoom(mid, DATA_DIR).history()
        ts = [s["ts"] for s in segs if isinstance(s.get("ts"), (int, float))]
        try:
            minutes = json.loads(info.read_text(encoding="utf-8")) if key[1] else None
        except ValueError:
            minutes = None
        if minutes is None and key[2]:
            minutes = {"status": "file_only"}
        summary = {"meeting_id": mid, "segments": len(segs), "speakers": sorted({str(s["speaker"]) for s in segs}),
                   "started": min(ts, default=None), "ended": max(ts, default=None), "minutes": minutes}
        _summary_cache[mid] = (key, summary)
    lock = _finalizing.get(mid)
    return {**summary, "live": mid in rooms, "finalizing": bool(lock and lock.locked())}


@app.get("/api/meetings")
async def meetings() -> list[dict]:
    rows = [_meeting_summary(p) for p in DATA_DIR.glob("*.jsonl") if MEETING_ID_RE.fullmatch(p.stem)]
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
    """Write the minutes once per press; presses that arrive while a run is going join that run's result."""
    _valid_id(meeting_id)
    lock = _finalizing.setdefault(meeting_id, asyncio.Lock())
    if lock.locked():
        async with lock:  # someone else is already writing these minutes: wait for them
            pass
        info = DATA_DIR / f"{meeting_id}.finalize.json"
        md = DATA_DIR / f"{meeting_id}.minutes.md"
        if info.exists() and md.exists():
            rec = json.loads(info.read_text(encoding="utf-8"))
            return {"meeting_id": meeting_id, "markdown": md.read_text(encoding="utf-8"), "llm": rec["llm"],
                    "llm_error": rec.get("llm_error"), "saved_to": str(md), "odoo": rec["odoo"], "joined": True}
    async with lock:
        return await _finalize(meeting_id)


async def _finalize(meeting_id: str) -> dict:
    segs = MeetingRoom(meeting_id, DATA_DIR).history()
    if not segs:
        raise HTTPException(404, "no completed captions for this meeting")
    llm_error = None
    try:
        markdown = await asyncio.to_thread(synthesize, segs, meeting_id, S.get("ollama_url"), S.get("ollama_model"))
        llm = True
    except SynthesisUnavailable as e:
        log.warning("%s: %s; falling back to transcript", meeting_id, e)
        markdown, llm, llm_error = transcript_markdown(segs), False, str(e)
    participants = sorted({s["speaker"] for s in segs})
    odoo = await asyncio.to_thread(odoo_client().publish_or_save, meeting_id, markdown, participants, DATA_DIR)
    (DATA_DIR / f"{meeting_id}.finalize.json").write_text(
        json.dumps({"finalized_at": time.time(), "llm": llm, "llm_model": S.get("ollama_model"), "llm_error": llm_error,
                    "odoo": odoo}), encoding="utf-8")
    if meeting_id in rooms:
        await rooms[meeting_id].broadcast({"type": "finalized", "meeting_id": meeting_id, "llm": llm,
                                           "odoo_status": odoo["status"]})
    return {"meeting_id": meeting_id, "markdown": markdown, "llm": llm, "llm_error": llm_error,
            "saved_to": odoo["path"], "odoo": odoo}


# --- live streams -------------------------------------------------------------------------------

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
    old = room.add_speaker(speaker, ws)
    if old is not None:
        # Newest connection wins: usually the same PC reconnecting after a network drop while the hub still
        # holds the dead socket. If it is a second PC with the same name, that one is told and stops (4001).
        log.info("%s: %s reconnected, replacing the previous connection", meeting_id, speaker)
        try:
            await asyncio.wait_for(old.send_json({"type": "error", "code": "replaced", "message":
                f"the name '{speaker}' is now used by another connection to meeting '{meeting_id}'"}), 1.0)
        except Exception:
            pass
        await _close(old, 4001)

    clock = StreamClock()

    # WhisperLive only finalises a sentence when more audio from the same person confirms it. In a meeting
    # people speak and then stay quiet for minutes, so their last sentence would hang "in progress" (or be
    # lost on disconnect). A partial unchanged for partial_commit_s is therefore made final here; measured on
    # the server, without this a 10-person turn-taking meeting kept only 42% of the words.
    pending: dict = {}          # current partial: start, frame, since
    committed: set[float] = set()  # starts already finalised by us; WhisperLive's later copy is skipped
    recent: deque = deque(maxlen=12)  # this person's last final sentences (normalised)

    def already_said(seg) -> bool:
        """WhisperLive keeps resending a sentence after it is final, sometimes with a shifted start;
        seen on the server as the same sentence stored up to 7 times. Short phrases ("ok", "sì") are
        exempt so that saying them twice is kept."""
        if seg.start in committed:
            return True
        words = normalize_text(seg.text)
        return len(words.split()) >= 4 and any(words == r or words in r for r in recent)

    def emit_completed(frame: dict) -> None:
        recent.append(normalize_text(frame["text"]))
        room.dedup.add(frame)
        task = asyncio.create_task(_settle(room, frame))
        _pending.add(task)
        task.add_done_callback(_pending.discard)

    def commit_pending() -> None:
        if pending:
            committed.add(pending["start"])
            emit_completed({**pending["frame"], "completed": True})
            pending.clear()

    async def on_segments(segs) -> None:
        for seg in segs:
            frame = caption_frame(meeting_id, speaker, seg, clock)
            if is_room_noise(frame["text"], frame["rms"], S.get("noise_floor_rms")):
                if seg.completed:  # clear a partial that screens may already show for this phantom sentence
                    if pending.get("start") == seg.start:
                        pending.clear()
                    await room.broadcast({"type": "retract", "meeting_id": meeting_id, "speaker": speaker})
                continue
            if seg.completed:
                if pending.get("start") == seg.start:
                    pending.clear()
                if already_said(seg):  # ponytail: WhisperLive's longer final text is dropped; fine for minutes
                    continue
                emit_completed(frame)
            else:
                if already_said(seg):
                    continue
                if pending.get("start") != seg.start or pending["frame"]["text"] != frame["text"]:
                    pending.update(start=seg.start, frame=frame, since=time.monotonic())
                await room.broadcast(frame)

    async def commit_stalled() -> None:
        while True:
            await asyncio.sleep(0.25)
            wait = S.get("partial_commit_s")
            if wait > 0 and pending and time.monotonic() - pending["since"] >= wait:
                commit_pending()

    transcriber = committer = None
    try:
        try:
            transcriber = make_transcriber(meeting_id, speaker, lang, kind=S.get("transcriber"),
                                           url=S.get("whisperlive_url"), model=S.get("whisper_model"))
            await transcriber.start(on_segments)
        except TranscriberUnavailable as e:
            await ws.send_json({"type": "error", "message": str(e)})
            return
        committer = asyncio.create_task(commit_stalled())
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
        if committer is not None:
            committer.cancel()
        if transcriber is not None:
            await transcriber.close()
        commit_pending()  # the last sentence of someone who leaves is kept, not lost
        room.remove_speaker(speaker, ws)
        await _close(ws)
        await room.broadcast(room.roster())
        _drop_if_empty(room)
