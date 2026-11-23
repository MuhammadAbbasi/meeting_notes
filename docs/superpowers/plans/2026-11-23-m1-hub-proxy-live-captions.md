# M1: Hub Proxy + Mock Transcriber + Live Captions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Live captions shared across workstations in one meeting, with the hub proxying audio to WhisperLive (or a deterministic mock), persisted to JSONL, visible in a web viewer and in a thin Windows overlay client.

**Architecture:** Client sends raw float32 PCM over one WebSocket to the FastAPI hub. The hub opens one upstream WhisperLive socket per speaker, keeps an audio-time clock + RMS per stream, turns upstream segment lists into once-only `caption` frames, broadcasts them to the room and appends completed ones to `data/meetings/<id>.jsonl`. `TRANSCRIBER=mock` swaps in a scripted transcriber so everything runs on the dev box.

**Tech Stack:** Python 3.11+ (hub), FastAPI + uvicorn + `websockets` + numpy; pytest + Starlette `TestClient`; client: `sounddevice`, `websockets`, tkinter; Docker Compose on the GPU host.

**Spec:** `docs/superpowers/specs/2026-11-23-meeting-suite-design.md` and `CONTRACTS.md` (the wire contracts are normative).

## Global Constraints

- Every JSON frame, field name and regex is exactly as in `CONTRACTS.md` §1–§3.
- Audio: `float32` LE, 16 000 Hz, mono; 4096-sample frames from the client.
- Hub env: `TRANSCRIBER` (`mock`|`whisperlive`, default `whisperlive`), `WHISPERLIVE_URL` (default `ws://whisperlive:9090`), `WHISPER_MODEL` (default `deepdml/faster-whisper-large-v3-turbo-ct2`), `DATA_DIR` (default `./data/meetings`), `MOCK_SCRIPT` (optional path).
- Python 3.11 syntax floor for `server/` and `client/` (no 3.12+-only features). Client pins 3.11/3.12 for the `.exe`.
- No new runtime dependency beyond: fastapi, uvicorn[standard], websockets, numpy (hub); sounddevice, websockets, numpy (client).
- Commit after each task.

## Review Focus

1. A binary frame whose length is not a multiple of 4 → hub must drop the frame and keep the stream alive, not crash the WebSocket. (Task 3 test `test_clock_rejects_misaligned_frame`, Task 4 test `test_misaligned_frame_does_not_kill_stream`.)
2. Two clients joining with the same speaker name in one room → second must be refused with `error` + close, not silently replace the first's socket. (Task 4 `test_duplicate_speaker_refused`.)
3. Upstream WhisperLive sends `WAIT` (server full) → client gets `{"type":"error"}` and the socket closes; no traceback, no half-open upstream. (Task 2 `test_tracker_wait_message_raises`, Task 4 covered by mock error path `test_transcriber_start_failure_sends_error`.)
4. A `caption` arriving after a listener's socket died → broadcast drops the dead socket and continues to the others. (Task 3 `test_broadcast_drops_dead_socket`.)
5. `meeting_id` like `../../etc` → closed with 1008, no file created outside `DATA_DIR`. (Task 4 `test_bad_meeting_id_rejected`.)

---

### Task 1: Repository scaffold and deployment files

**Files:**
- Create: `.gitignore`, `requirements-dev.txt`, `pytest.ini`, `docker-compose.yml`, `.env.example`, `server/Dockerfile`, `server/requirements.txt`, `server/__init__.py`, `tests/__init__.py`

**Interfaces:**
- Produces: compose services `whisperlive` (9090), `ollama` (11434), `caption-hub` (8085) on network `meetnet`; hub container runs `uvicorn server.hub:app --host 0.0.0.0 --port 8085` from `/app`.

- [x] **Step 1: Write `.gitignore`** – `.venv/`, `__pycache__/`, `data/`, `.env`, `client/config.json`, `build/`, `dist/`, `*.spec`.
- [x] **Step 2: Write `server/requirements.txt`** (`fastapi`, `uvicorn[standard]`, `websockets`, `numpy`) and `requirements-dev.txt` (`-r server/requirements.txt`, `pytest`, `httpx`).
- [x] **Step 3: Write `pytest.ini`** with `testpaths = tests` and `pythonpath = .`.
- [x] **Step 4: Write `docker-compose.yml`** with the three services, GPU reservations for `whisperlive` and `ollama`, volumes `hf-cache:/root/.cache`, `ollama:/root/.ollama`, `./data/meetings:/app/data/meetings`, and the WhisperLive command from CONTRACTS §2.
- [x] **Step 5: Write `.env.example`** with `SERVER_IP=192.168.10.17`, `WHISPER_MODEL=deepdml/faster-whisper-large-v3-turbo-ct2`, `OLLAMA_MODEL=qwen3.5:9b`, `ODOO_URL=`, `ODOO_DB=`, `ODOO_USER=`, `ODOO_API_KEY=`, `ODOO_PROJECT_ID=`.
- [x] **Step 6: Write `server/Dockerfile`** – `python:3.12-slim`, install `server/requirements.txt`, copy `server/`, CMD uvicorn as above.
- [ ] **Step 7: Verify** – Run: `docker compose config` on a machine with Docker running. Expected: rendered config, no error. (On the dev box Docker is stopped; record as deferred.)
- [x] **Step 8: Commit** `chore: scaffold repo, contracts, spec, compose`.

### Task 2: Transcriber layer (`server/transcriber.py`)

**Files:**
- Create: `server/transcriber.py`
- Test: `tests/test_transcriber.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Segment(start: float, end: float, text: str, completed: bool)`
  - `OnSegments = Callable[[list[Segment]], Awaitable[None]]`
  - `class SegmentTracker: def ingest(self, segments: list[dict]) -> list[Segment]` – returns, in order, every not-yet-seen `completed:true` segment (keyed by the `start` string) followed by the trailing partial if present (stripped text; partial with empty text is skipped).
  - `def build_handshake(uid: str, language: str | None, model: str) -> dict` – exactly CONTRACTS §2.1.
  - `class Transcriber(Protocol): kind: str; async def start(self, on_segments: OnSegments) -> None; async def feed(self, pcm: bytes) -> None; async def end(self) -> None; async def close(self) -> None`
  - `class MockTranscriber(speaker: str, script: list[str] | None = None)` – `kind="mock"`, behaviour CONTRACTS §3.
  - `class WhisperLiveTranscriber(url: str, uid: str, language: str | None, model: str)` – `kind="whisperlive"`; `start()` connects, sends handshake, awaits `SERVER_READY`; raises `TranscriberUnavailable(msg)` on `WAIT` or connect failure; reader task calls `on_segments(tracker.ingest(msg["segments"]))` and ignores other messages; `end()` sends `b"END_OF_AUDIO"` and waits ≤ 2 s for the reader to finish; `close()` cancels the reader and closes the socket.
  - `class TranscriberUnavailable(Exception)`
  - `def make_transcriber(meeting_id: str, speaker: str, language: str) -> Transcriber` – reads `TRANSCRIBER`, `WHISPERLIVE_URL`, `WHISPER_MODEL`, `MOCK_SCRIPT`; `language == "auto"` → `None`; uid `f"{meeting_id}:{speaker}:{uuid4().hex[:8]}"`.

- [x] **Step 1: Write failing tests** in `tests/test_transcriber.py`:
  - `test_tracker_emits_completed_once`: ingest `[{"start":"0.000","end":"1.000","text":" a ","completed":True}]` twice → first returns `[Segment(0.0,1.0,"a",True)]`, second `[]`.
  - `test_tracker_partial_follows_completed`: ingest completed A + partial B → `[A, B(completed=False)]`; ingest again with same A and partial B2 → `[B2]`.
  - `test_tracker_skips_empty_partial`: partial with text `"  "` → `[]`.
  - `test_build_handshake_matches_contract`: `build_handshake("m:s:abcd1234", None, "x/y")` equals the §2.1 dict literal.
  - `test_mock_emits_partial_then_completed` (asyncio): feed 16 000 float32 zero samples → callback got `[Segment(0,1,"Muhammad seg",False)]`-shaped partial (text = first half of `"Muhammad segment 1"`), feed 16 000 more → `[Segment(0.0,2.0,"Muhammad segment 1",True)]`.
  - `test_mock_uses_script`: script `["ciao", "come va"]` → first completed text `"ciao"`, second `"come va"`, third falls back to `"Muhammad segment 3"`.
  - `test_make_transcriber_mock_from_env` (monkeypatch `TRANSCRIBER=mock`) → `kind == "mock"`.
- [x] **Step 2: Run** `.venv/Scripts/python -m pytest tests/test_transcriber.py -q` → FAIL (import error).
- [x] **Step 3: Implement `server/transcriber.py`** per Interfaces. Mock: count samples; at ≥16 000 since block start and no partial yet → partial; at ≥32 000 → completed, reset block.
- [x] **Step 4: Run tests** → PASS.
- [x] **Step 5: Commit** `feat(server): transcriber layer with mock and WhisperLive adapter`.

### Task 3: Meeting room, audio clock and persistence (`server/meeting.py`)

**Files:**
- Create: `server/meeting.py`
- Test: `tests/test_meeting.py`

**Interfaces:**
- Consumes: `Segment` from Task 2.
- Produces:
  - `class StreamClock(sample_rate: int = 16000)`: `start_ts: float | None` (set on first `add`), `audio_time: float` (seconds received), `def add(self, pcm: bytes) -> float` (returns chunk RMS; raises `ValueError` if `len(pcm) % 4`), `def rms_between(self, start: float, end: float) -> float` (mean of chunk RMS values whose chunk time ∈ [start, end), 0.0 if none), `def abs_time(self, start: float) -> float` (`start_ts + start`).
  - `class MeetingRoom(meeting_id: str, data_dir: Path)`: `speakers: dict[str, Any]` (name → socket), `listeners: set[Any]`; `add_speaker(name, ws) -> bool` (False if name taken), `remove_speaker(name)`, `add_listener(ws)`, `remove_listener(ws)`, `roster() -> dict` (the §1.1 roster frame), `async broadcast(msg: dict)` (send_json to all sockets, remove ones that raise), `record(caption: dict)` (append JSON line of §1.3 fields to `data_dir/<id>.jsonl`), `history() -> list[dict]`.
  - `def caption_frame(meeting_id, speaker, seg: Segment, clock: StreamClock) -> dict` – the §1.1 caption dict.
  - `MEETING_ID_RE`, `SPEAKER_RE` compiled from CONTRACTS §1.1.

- [x] **Step 1: Write failing tests** `tests/test_meeting.py`:
  - `test_clock_counts_audio_time_and_rms`: add 16 000 samples of 0.5 → `audio_time == 1.0`, returned rms ≈ 0.5, `rms_between(0, 1) ≈ 0.5`, `rms_between(5, 6) == 0.0`.
  - `test_clock_rejects_misaligned_frame`: `add(b"\x00\x00\x00")` raises `ValueError`, `audio_time` unchanged.
  - `test_caption_frame_fields`: with `start_ts=1000.0`, `Segment(2.0,3.5,"hi",True)` → dict has `ts == 1002.0`, keys exactly `{"type","meeting_id","speaker","text","completed","start","end","ts","rms"}`.
  - `test_room_record_and_history` (tmp_path): record two captions → `history()` returns them with only §1.3 fields; file has 2 lines.
  - `test_room_duplicate_speaker`: `add_speaker("A", ws1)` True, `add_speaker("A", ws2)` False.
  - `test_broadcast_drops_dead_socket` (asyncio): fake sockets where one `send_json` raises → after broadcast it is removed and the other got the message.
- [x] **Step 2: Run** → FAIL.
- [x] **Step 3: Implement `server/meeting.py`**. RMS history as a list of `(chunk_start_time, rms)`; `ponytail:` comment on the linear scan (fine for ≤ 4 h at 256 ms).
- [x] **Step 4: Run** → PASS.
- [x] **Step 5: Commit** `feat(server): meeting room, audio clock, JSONL persistence`.

### Task 4: Hub app (`server/hub.py`) and web viewer

**Files:**
- Create: `server/hub.py`, `server/static/captions.html`
- Test: `tests/test_hub.py`

**Interfaces:**
- Consumes: Task 2 `make_transcriber`, `TranscriberUnavailable`; Task 3 `MeetingRoom`, `StreamClock`, `caption_frame`, regexes.
- Produces: FastAPI `app` with routes in CONTRACTS §1.1–1.3; module-level `rooms: dict[str, MeetingRoom]` and `get_room(meeting_id) -> MeetingRoom`; `DATA_DIR` read from env at import.

- [x] **Step 1: Write failing tests** `tests/test_hub.py` (fixture: `monkeypatch.setenv("TRANSCRIBER","mock")`, `setenv("DATA_DIR", tmp_path)`, `importlib.reload(server.hub)`, `TestClient(app)`); helper `pcm(seconds)` → zero float32 bytes:
  - `test_health` → 200, `transcriber == "mock"`.
  - `test_speaker_gets_ready_then_roster`: connect `/ws/daily/Muhammad` → first frame `ready` (speaker, transcriber mock), next `roster` with `["Muhammad"]`.
  - `test_caption_broadcast_to_listener_and_other_speaker`: Muhammad + Gabriele + listener `/listen/daily`; Muhammad sends `pcm(2.0)` → listener and Gabriele each receive a partial then a completed caption with `speaker=="Muhammad"`, `text=="Muhammad segment 1"`, `start==0.0`, `end==2.0`, `rms==0.0`.
  - `test_completed_caption_persisted`: after the above, `GET /api/meetings/daily/segments` → 1 item, and `data/meetings/daily.jsonl` exists under tmp.
  - `test_duplicate_speaker_refused`: second `/ws/daily/Muhammad` gets `{"type":"error",...}` then disconnect; first still receives captions.
  - `test_bad_meeting_id_rejected`: `/ws/..%2F..%2Fetc/A` and `/ws/x y/A` → `WebSocketDisconnect` with code 1008; no file written.
  - `test_misaligned_frame_does_not_kill_stream`: send 3 bytes then `pcm(2.0)` → completed caption still arrives.
  - `test_end_flushes_and_closes`: send `{"type":"end"}` → socket closes (`WebSocketDisconnect`), roster broadcast to listener no longer contains the speaker.
  - `test_transcriber_start_failure_sends_error`: monkeypatch `server.hub.make_transcriber` to raise `TranscriberUnavailable("full")` → client gets `error` with `"full"` in message, then disconnect.
  - `test_viewer_served`: `GET /captions/daily` → 200, `text/html`, contains `/listen/daily`.
- [x] **Step 2: Run** → FAIL.
- [x] **Step 3: Implement `server/hub.py`**: speaker endpoint flow = accept → validate (1008) → `add_speaker` or `error`+close → `make_transcriber` → `start` (on failure `error`+close) → `ready` → roster broadcast → receive loop (bytes: `clock.add` in try/except ValueError → `transcriber.feed`; text `end` → `transcriber.end()` → break) → finally `close`, `remove_speaker`, roster broadcast. `on_segments` builds `caption_frame`, `record`s completed ones, `broadcast`s each. Listener endpoint: validate, `add_listener`, send roster, wait for disconnect.
- [x] **Step 4: Write `server/static/captions.html`**: single file, no build; connects to `/listen/{id}` via the page's host, loads `/api/meetings/{id}/segments` first, one colour per speaker (hash → hue), partials in muted text, dark/light via `prefers-color-scheme`.
- [x] **Step 5: Run** `pytest tests -q` → PASS.
- [x] **Step 6: Manual check**: `TRANSCRIBER=mock .venv/Scripts/python -m uvicorn server.hub:app --port 8085`, open `http://127.0.0.1:8085/captions/daily` → page loads, roster empty.
- [x] **Step 7: Commit** `feat(server): FastAPI hub with speaker/listener websockets and viewer`.

### Task 5: Thin Windows client (`client/`)

**Files:**
- Create: `client/office_agent.py`, `client/audio_capture.py`, `client/overlay_ui.py`, `client/hub_link.py`, `client/config.example.json`, `client/requirements.txt`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: CONTRACTS §1.1 only.
- Produces:
  - `audio_capture.MicCapture(device: int | str | None = None, samplerate: int = 16000, blocksize: int = 4096)`: `start()`, `stop()`, `read(timeout: float = 1.0) -> bytes | None` (float32 LE bytes), `muted: bool` (when True, `read` returns zero bytes of the same length so the stream clock keeps running).
  - `hub_link.backoff_delays() -> Iterator[float]` yields `1, 2, 4, 8, 16, 30, 30, ...`.
  - `hub_link.HubLink(url: str, audio: MicCapture, on_message: Callable[[dict], None])`: `run()` blocking loop with reconnect; `stop()`; `url` built by `hub_link.speaker_url(server_ip, port, meeting_id, speaker, language)` with URL-encoded speaker.
  - `overlay_ui.CaptionOverlay(on_mute: Callable[[bool], None])`: tkinter window per spec (frameless, topmost, alpha 0.90, bottom-centre, draggable); `push(frame: dict)` thread-safe; buttons Mute, Hide; `run()`.
  - `office_agent.main()` reads `client/config.json` (copy of `config.example.json`), starts capture, HubLink in a thread, overlay in main thread.
  - `config.example.json`: `{"user_name":"Muhammad","server_ip":"192.168.10.17","hub_port":8085,"meeting_id":"daily","language":"auto","input_device":null}`.

- [x] **Step 1: Write failing tests** `tests/test_client.py`:
  - `test_backoff_sequence`: first 7 values `[1,2,4,8,16,30,30]`.
  - `test_speaker_url_encodes_name`: `speaker_url("192.168.10.17", 8085, "daily", "Gian Domenico", "auto") == "ws://192.168.10.17:8085/ws/daily/Gian%20Domenico?lang=auto"`.
  - `test_muted_capture_returns_silence`: `MicCapture` with injected fake stream (constructor arg `_stream_factory`) → after `muted=True`, `read()` returns `blocksize*4` zero bytes.
- [x] **Step 2: Run** → FAIL.
- [x] **Step 3: Implement the four client modules** per Interfaces. `HubLink.run`: `websockets.sync.client.connect`, a sender thread reading `audio.read()` and `send(bytes)`, receiver loop parsing JSON → `on_message`; on any exception sleep next backoff, on success reset backoff.
- [x] **Step 4: Run** → PASS.
- [x] **Step 5: Manual check on the dev box**: hub in mock mode, `python client/office_agent.py` → overlay shows `Muhammad segment 1`, `…2` every 2 s; viewer page shows the same.
- [x] **Step 6: Commit** `feat(client): mic capture, hub link with reconnect, caption overlay`.

### Task 6: Multi-user simulator (`tests/simulate_multi_user_meeting.py`)

**Files:**
- Create: `tests/simulate_multi_user_meeting.py`, `tests/mock_script.json`

**Interfaces:**
- Consumes: CONTRACTS §1.1–1.3.
- Produces: CLI `python tests/simulate_multi_user_meeting.py --hub ws://127.0.0.1:8085 --meeting sim --speakers Muhammad,Gabriele,Ginadomenico [--wav path.wav] [--seconds 8]`; exit 0 if every speaker's completed captions were received by a listener and `GET /api/meetings/sim/segments` count matches, else exit 1 with a diff.

- [x] **Step 1: Write `tests/mock_script.json`** – three speakers × 4 Italian/English lines (realistic PV-plant dialogue).
- [x] **Step 2: Implement the simulator** (asyncio + `websockets`): one listener, N speakers streaming 256 ms frames at real-time pace (synthetic 440 Hz tone at 0.2 amplitude, or `--wav` resampled with numpy to 16 kHz float32), `{"type":"end"}` at the end, assertions as above.
- [x] **Step 3: Verify**: hub running with `TRANSCRIBER=mock MOCK_SCRIPT=tests/mock_script.json`; run the simulator → prints captions per speaker, `OK`, exit 0.
- [x] **Step 4: Commit** `test: multi-user meeting simulator`.

### Task 7: Milestone gate

- [x] **Step 1: Run** `.venv/Scripts/python -m pytest -q` → all PASS.
- [x] **Step 2: Run** `.venv/Scripts/python -m compileall -q server client tests` → no output.
- [ ] **Step 3: Deferred to the GPU host:** `docker compose up -d`, then the simulator with `--wav` against `ws://192.168.10.17:8085` and `TRANSCRIBER=whisperlive`. Record result in the M1 handoff.
- [x] **Step 4: Commit** `chore: M1 gate`.
