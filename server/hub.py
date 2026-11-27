"""Caption hub: proxies workstation audio to the transcriber and fans captions out per meeting.

Wire protocol: CONTRACTS.md §1. Run: uvicorn server.hub:app --host 0.0.0.0 --port 8085
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
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

from server.deduplicator import CrossTalkFilter, is_room_noise, merge_internal_and_remote, normalize_text
from server.docker_api import exec_container, restart_container
from server.meeting import MEETING_ID_RE, SPEAKER_RE, AudioRecorder, MeetingRoom, StreamClock, caption_frame
from server.odoo_client import OdooMeetingClient
from server.ollama_synthesizer import SynthesisUnavailable, synthesize, transcript_markdown
from server.settings import FIELDS, Settings
from server.transcriber import TranscriberUnavailable, make_transcriber

log = logging.getLogger("hub")
DATA_DIR = Path(os.getenv("DATA_DIR", "./data/meetings"))
EXPORT_DIR = Path(os.getenv("EXPORT_DIR")) if os.getenv("EXPORT_DIR") else None
DIST_DIR = Path(os.getenv("DIST_DIR", "./data/dist"))
SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", "./data/settings.json"))
WHISPER_MODEL_FILE = Path(os.getenv("WHISPER_MODEL_FILE", "./data/config/whisper_model"))
WHISPERLIVE_CONTAINER = os.getenv("WHISPERLIVE_CONTAINER", "caption-whisperlive")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
EXE_NAME = "OfficeMeetingAgent.exe"
HUB_VERSION = "2026.11.25-6"  # bump on every deploy-relevant change; /health reports it
_STATIC = Path(__file__).parent / "static"
VIEWER_HTML = (_STATIC / "captions.html").read_text(encoding="utf-8")
DASHBOARD_HTML = (_STATIC / "dashboard.html").read_text(encoding="utf-8")
HOME_HTML = (_STATIC / "home.html").read_text(encoding="utf-8")

app = FastAPI(title="caption-hub")
S = Settings(SETTINGS_FILE)
rooms: dict[str, MeetingRoom] = {}
_pending: set[asyncio.Task] = set()  # keeps hold-window tasks alive
_finalizing: dict[str, asyncio.Lock] = {}  # one minutes run per meeting, however many people press the button
_summary_cache: dict[str, tuple[tuple, dict]] = {}  # meeting_id -> (file stats, summary)
_calendar_meta: dict[str, dict] = {}  # meeting_id -> {project_id, attendees} from Odoo calendar
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
    v = {k: S.get(k) for k in ("odoo_url", "odoo_db", "odoo_user", "odoo_api_key", "odoo_project_id",
                               "odoo_knowledge_parent", "speaker_aliases")}
    v.update({k: x for k, x in overrides.items() if k in v and x not in (None, "")})
    return OdooMeetingClient(url=v["odoo_url"], db=v["odoo_db"], username=v["odoo_user"], api_key=v["odoo_api_key"],
                             project_id=v["odoo_project_id"], knowledge_parent=v["odoo_knowledge_parent"],
                             speaker_aliases=v["speaker_aliases"])


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


ECHO_PAD_S = 3.0  # recognizer timestamps scatter by a few seconds (measured), so compare a padded window


def is_echo(room: MeetingRoom, speaker: str, clock: StreamClock, frame: dict) -> bool:
    """Same-room echo: another microphone was `same_room_ratio` times louder around this caption, so this
    microphone only heard someone else across the room. Text matching misses these (each microphone splits
    speech differently). Measured on a real 10-person one-room run: 92% of echo captions removed; with
    headsets nothing is removed. Two people talking at once each stay loudest on their own microphone."""
    ratio = S.get("same_room_ratio")
    if ratio <= 0 or len(room.clocks) < 2 or clock.start_ts is None:
        return False
    a, b = frame["ts"] - ECHO_PAD_S, frame["ts"] + (frame["end"] - frame["start"]) + ECHO_PAD_S
    mine = clock.peak_between(a - clock.start_ts, b - clock.start_ts)
    loudest = max((c.peak_between(a - c.start_ts, b - c.start_ts) for name, c in list(room.clocks.items())
                   if name != speaker and c.start_ts is not None), default=0.0)
    return loudest >= ratio * max(mine, 1e-4)


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

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root() -> HTMLResponse:
    return HTMLResponse(HOME_HTML, headers=NO_CACHE)


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


@app.get("/api/odoo/projects", dependencies=[Depends(admin)])
async def odoo_projects() -> dict:
    client = odoo_client()
    if not client.is_configured:
        return {"projects": []}
    try:
        return {"projects": await asyncio.to_thread(client.fetch_projects)}
    except Exception:
        return {"projects": []}


@app.get("/api/odoo/users")
async def odoo_users() -> dict:
    """Active internal Odoo users (name + email) for client first-run matching."""
    client = odoo_client()
    if not client.is_configured:
        return {"users": []}
    try:
        rows = await asyncio.to_thread(client.fetch_active_users)
        return {"users": [{"id": int(u["id"]), "name": u["name"], "email": u.get("email") or u.get("login") or ""}
                          for u in rows]}
    except Exception:
        return {"users": []}


@app.get("/api/calendar")
async def calendar() -> dict:
    """Today's Odoo calendar events merged with any live rooms (never fails if Odoo is unreachable)."""
    events: list[dict] = []
    client = odoo_client()
    if client.is_configured:
        try:
            events = await asyncio.to_thread(client.fetch_upcoming_events)
        except Exception as e:
            log.warning("calendar fetch failed: %s", e)
    seen: set[str] = set()
    out: list[dict] = []
    for ev in events:
        mid = ev["meeting_id"]
        if mid in seen:
            continue
        seen.add(mid)
        _calendar_meta[mid] = {"project_id": ev.get("project_id"), "attendees": ev.get("attendees") or []}
        room = rooms.get(mid)
        out.append({**ev, "live_speakers": sorted(room.speakers) if room else []})
    for mid, room in sorted(rooms.items()):
        if mid not in seen and room.speakers:
            out.append({"meeting_id": mid, "title": mid, "start": None, "stop": None,
                        "project_id": None, "project_name": None, "attendees": [],
                        "live_speakers": sorted(room.speakers)})
    return {"meetings": out}


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
    return {"user_name": "", "user_email": "", "server_ip": request.url.hostname, "hub_port": request.url.port or 80,
            "meeting_id": S.get("default_meeting"), "language": S.get("default_language"),
            "input_device": None, "overlay_alpha": 0.5, "capture_system_audio": True,
            "notes_dir": "C:/a176lab_meetings"}


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
    room = rooms.get(mid)
    return {**summary, "live": bool(room and room.speakers), "finalizing": bool(lock and lock.locked())}


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
async def finalize(meeting_id: str, request: Request) -> dict:
    """Write the minutes once per press; presses that arrive while a run is going join that run's result."""
    _valid_id(meeting_id)
    raw = await request.body()
    opts = json.loads(raw) if raw.strip() else {}
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
        return await _finalize(meeting_id, project_id=opts.get("project_id"),
                               attendees=opts.get("attendees"), language=opts.get("language"),
                               publish=opts.get("publish", True))


_RETRANSCRIBE_PY = """
import json, pathlib, sys
from faster_whisper import WhisperModel
audio_dir, model_id, lang = pathlib.Path(sys.argv[1]), sys.argv[2], (None if sys.argv[3] == "auto" else sys.argv[3])
m = WhisperModel(model_id, device="cuda", compute_type="float16")
rows = []
for wav in sorted(audio_dir.glob("*.wav")):
    sp = wav.stem.replace("_", " ")
    segs, _ = m.transcribe(str(wav), language=lang, beam_size=5, vad_filter=True, condition_on_previous_text=False)
    for s in segs:
        t = s.text.strip()
        if t:
            rows.append({"speaker": sp, "text": t, "start": float(s.start), "end": float(s.end), "ts": wav.stat().st_mtime + float(s.start), "rms": 0.1})
rows.sort(key=lambda r: r["ts"])
print(json.dumps(rows, ensure_ascii=False))
""".strip()


def retranscribe_audio(meeting_id: str, lang: str) -> list[dict] | None:
    """Batch re-transcribe saved WAVs in WhisperLive container for higher accuracy; returns None if unavailable."""
    if S.get("transcriber") == "mock":
        return None
    audio_dir = DATA_DIR / "audio" / meeting_id
    if not audio_dir.exists() or not any(audio_dir.glob("*.wav")):
        return None
    try:
        out = exec_container(WHISPERLIVE_CONTAINER, [
            "python", "-c", _RETRANSCRIBE_PY, f"/app/data/meetings/audio/{meeting_id}", S.get("whisper_model"), lang
        ])
        for line in reversed(out.splitlines()):
            if line.strip().startswith("["):
                rows = json.loads(line.strip())
                return [r for r in rows if not is_room_noise(r["text"], r["rms"], S.get("noise_floor_rms"))] or None
    except Exception as e:
        log.warning("%s: batch re-transcription skipped (%s)", meeting_id, e)
    return None


async def _finalize(meeting_id: str, project_id: int | None = None, attendees: list[str] | None = None,
                    language: str | None = None, publish: bool = True) -> dict:
    if _pending:  # flush in-flight hold-window captions so the final sentence isn't dropped
        await asyncio.gather(*list(_pending), return_exceptions=True)
    lang = language or S.get("default_language") or "it"
    segs = await asyncio.to_thread(retranscribe_audio, meeting_id, lang) or MeetingRoom(meeting_id, DATA_DIR).history()
    if not segs:
        raise HTTPException(404, "no completed captions for this meeting")
    segs = merge_internal_and_remote(segs)
    llm_error = None
    try:
        # 10 min: on a GPU shared with live recognition a long meeting took over the old 5 min (measured)
        markdown = await asyncio.to_thread(synthesize, segs, meeting_id, S.get("ollama_url"), S.get("ollama_model"), 600,
                                           lang)
        llm = True
    except SynthesisUnavailable as e:
        log.warning("%s: %s; falling back to transcript", meeting_id, e)
        markdown, llm, llm_error = transcript_markdown(segs), False, str(e)
    participants = sorted({s["speaker"] for s in segs})
    meta = _calendar_meta.get(meeting_id, {})
    target_project = project_id if project_id is not None else meta.get("project_id")
    target_attendees = attendees if attendees is not None else meta.get("attendees")
    if publish:
        odoo = await asyncio.to_thread(odoo_client().publish_or_save, meeting_id, markdown, participants, DATA_DIR,
                                       project_id=target_project, attendees=target_attendees)
        if EXPORT_DIR is not None:
            try:
                EXPORT_DIR.mkdir(parents=True, exist_ok=True)
                (EXPORT_DIR / f"{meeting_id}.minutes.md").write_text(markdown, encoding="utf-8")
            except OSError as e:
                log.warning("export write failed: %s", e)
    else:
        md_path = DATA_DIR / f"{meeting_id}.minutes.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")
        odoo = {"status": "draft", "path": str(md_path)}
    (DATA_DIR / f"{meeting_id}.finalize.json").write_text(
        json.dumps({"finalized_at": time.time(), "llm": llm, "llm_model": S.get("ollama_model"), "llm_error": llm_error,
                    "odoo": odoo}), encoding="utf-8")
    if publish and meeting_id in rooms:
        await rooms[meeting_id].broadcast({"type": "finalized", "meeting_id": meeting_id, "llm": llm,
                                           "odoo_status": odoo["status"]})
    return {"meeting_id": meeting_id, "markdown": markdown, "segments": segs, "llm": llm, "llm_error": llm_error,
            "saved_to": odoo["path"], "odoo": odoo}


@app.post("/api/meetings/{meeting_id}/publish")
async def publish_meeting(meeting_id: str, request: Request) -> dict:
    """Publish already-generated (or user-edited) minutes to Odoo without re-running Ollama."""
    _valid_id(meeting_id)
    raw = await request.body()
    opts = json.loads(raw) if raw.strip() else {}
    md_path = DATA_DIR / f"{meeting_id}.minutes.md"
    if opts.get("markdown"):
        markdown = str(opts["markdown"])
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")
    elif md_path.exists():
        markdown = md_path.read_text(encoding="utf-8")
    else:
        raise HTTPException(404, "meeting has no generated minutes yet")
    if isinstance(opts.get("segments"), list):
        jsonl = DATA_DIR / f"{meeting_id}.jsonl"
        jsonl.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in opts["segments"]), encoding="utf-8")
    segs = MeetingRoom(meeting_id, DATA_DIR).history()
    participants = sorted({s["speaker"] for s in segs})
    meta = _calendar_meta.get(meeting_id, {})
    odoo = await asyncio.to_thread(odoo_client().publish_or_save, meeting_id, markdown, participants, DATA_DIR,
                                   project_id=meta.get("project_id"), attendees=meta.get("attendees"))
    if EXPORT_DIR is not None:
        try:
            EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            (EXPORT_DIR / f"{meeting_id}.minutes.md").write_text(markdown, encoding="utf-8")
        except OSError as e:
            log.warning("export write failed: %s", e)
    info_path = DATA_DIR / f"{meeting_id}.finalize.json"
    rec = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {}
    rec.update(odoo=odoo)
    info_path.write_text(json.dumps(rec), encoding="utf-8")
    if meeting_id in rooms:
        await rooms[meeting_id].broadcast({"type": "finalized", "meeting_id": meeting_id,
                                           "llm": rec.get("llm", True), "odoo_status": odoo["status"]})
    return {"meeting_id": meeting_id, "odoo": odoo}


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
    if speaker == "Remote-Call" and "Remote-Call" in room.speakers:
        await ws.send_json({"type": "error", "code": "loopback_active",
                            "message": "another computer is already captioning call audio in this meeting"})
        await _close(ws, 4002)
        return
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
    room.clocks[speaker] = clock

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
            if is_room_noise(frame["text"], frame["rms"], S.get("noise_floor_rms")) or is_echo(room, speaker, clock, frame):
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

    transcriber = committer = recorder = None
    try:
        try:
            transcriber = make_transcriber(meeting_id, speaker, lang, kind=S.get("transcriber"),
                                           url=S.get("whisperlive_url"), model=S.get("whisper_model"))
            await transcriber.start(on_segments)
        except TranscriberUnavailable as e:
            await ws.send_json({"type": "error", "message": str(e)})
            return
        safe_sp = re.sub(r"[^A-Za-z0-9_.-]+", "_", speaker).strip("_") or "speaker"
        recorder = AudioRecorder(DATA_DIR / "audio" / meeting_id / f"{safe_sp}.wav")
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
                recorder.write(msg["bytes"])
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
        if recorder is not None:
            recorder.close()
        if transcriber is not None:
            await transcriber.close()
        commit_pending()  # the last sentence of someone who leaves is kept, not lost
        room.remove_speaker(speaker, ws)
        if room.clocks.get(speaker) is clock:
            room.clocks.pop(speaker, None)
        await _close(ws)
        await room.broadcast(room.roster())
        _drop_if_empty(room)
