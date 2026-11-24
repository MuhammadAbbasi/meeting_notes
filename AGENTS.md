# Working on this repo (humans and AI agents)

Office meeting captions + minutes: Windows agent → FastAPI hub → WhisperLive (GPU) / Ollama / Odoo.
`README.md` explains what it does and how to run it; this file explains how to change it.

## Map

| Path | What | Notes |
| --- | --- | --- |
| `CONTRACTS.md` | **Normative** wire contracts: client↔hub WebSocket frames, HTTP API, persistence, dedup rules, WhisperLive protocol, mock behaviour | Change the contract first, then code and tests |
| `server/hub.py` | FastAPI app: `/ws`, `/listen`, `/captions`, `/dashboard`, `/api/*`, `/download/*`, finalize | Single process, asyncio; blocking work goes through `asyncio.to_thread` |
| `server/settings.py` | `FIELDS` = every editable setting (env default, type, secret flag); `data/settings.json` overrides env | Add a setting here + a form field in `dashboard.html`; nothing else |
| `server/transcriber.py` | `SegmentTracker`, `MockTranscriber`, `WhisperLiveTranscriber`, `make_transcriber` | Mock emits one scripted line per 2 s of audio |
| `server/meeting.py` | `StreamClock` (audio-time + RMS), `MeetingRoom` (speakers, listeners, JSONL) | |
| `server/deduplicator.py` | `CrossTalkFilter`: same sentence from two mics within 2 s → quieter one retracted | Pure functions, easy to unit test |
| `server/ollama_synthesizer.py` | prompt, `/api/chat` call (stdlib urllib), `<think>`/fence stripping, transcript fallback | |
| `server/odoo_client.py` | Odoo 18 XML-RPC: parent task + per-owner sub-tasks, speaker→user resolution, `publish_or_save` | Ported from the user's reference; `@` kept in speaker labels on purpose |
| `server/docker_api.py` | restart a container over the Docker socket (used for Whisper model changes) | |
| `server/static/dashboard.html` | admin page: login form, settings, tests, agent upload/download, meeting history | Vanilla JS, no build step; the hub reads it at import time (restart to see edits) |
| `server/static/captions.html` | public live caption viewer | |
| `server/whisperlive.Dockerfile` | upstream WhisperLive image + CUDA 12 cuBLAS/cuDNN (upstream ships CUDA 13, ctranslate2 needs 12) | |
| `client/` | Windows agent: `office_agent.py` (Agent class), `audio_capture.py` (mic + WASAPI loopback), `hub_link.py`, `overlay_ui.py` (tk strip), `tray.py` (pystray), `setup_dialog.py`, `build_exe.bat` | |
| `tests/` | pytest (hub via Starlette `TestClient`, mock transcriber) + `simulate_multi_user_meeting.py` (real hub, N speakers, WAV or tones, noise, bleed, finalize) | |
| `docs/superpowers/` | design spec and the two implementation plans (all steps done) | History, not instructions |

## Rules

1. **Contract first.** Anything on the wire or on disk is in `CONTRACTS.md`. Update it in the same commit.
2. **Tests run without a GPU.** `TRANSCRIBER=mock` everywhere in pytest; WhisperLive/Ollama/Odoo are
   reached only through small seams (`make_transcriber`, `synthesize`, `_ollama_models`, `restart_container`,
   `OdooMeetingClient._execute`) that tests monkeypatch. Keep it that way.
3. **No new hub runtime dependency** without a reason stated in the commit (today: fastapi, uvicorn,
   websockets, numpy). Stdlib `urllib`/`xmlrpc`/`http.client` are used on purpose.
4. **Settings, not env.** New knobs go into `server/settings.py` `FIELDS` so the dashboard can edit them
   live for every workstation; env vars are first-run defaults only.
5. **Secrets never leave the hub.** `Settings.public()` masks them; API responses are checked by tests
   (`"supersecret" not in r.text`). Admin endpoints carry `dependencies=[Depends(admin)]`.
6. **Minimal code** ("ponytail" style): deliberate shortcuts are marked with a `# ponytail:` comment naming
   the ceiling and the upgrade path. Don't add abstractions for one caller.
7. **Commits**: conventional prefix (`feat(server):`, `fix(compose):`, `docs:`), body says what was
   verified. Push to `main` on `github.com/MuhammadAbbasi/meeting_notes`.

## Daily commands

```powershell
.venv\Scripts\python -m pytest -q                                   # ~70 tests, ~30 s
$env:TRANSCRIBER="mock"; $env:MOCK_SCRIPT="tests\mock_script.json"; $env:ADMIN_PASSWORD="pw"
.venv\Scripts\python -m uvicorn server.hub:app --port 8085           # local hub, dashboard at /dashboard
.venv\Scripts\python tests\simulate_multi_user_meeting.py            # 3 fake speakers → OK/FAIL
.venv\Scripts\python tests\simulate_multi_user_meeting.py --hub ws://192.168.10.17:8085 --wav x.wav --finalize
```

Windows text-to-speech makes usable test WAVs (16 kHz mono) when no recording is at hand:
`System.Speech.Synthesis.SpeechSynthesizer` with voices "Microsoft Zira" (en) / "Microsoft Cosimo" (it).

## The office deployment (facts you cannot derive from the code)

- GPU host `192.168.10.17` (RTX 4060 Ti 16 GB, 64 GB RAM, Windows 11, Docker Desktop). Repo checkout:
  `C:\Users\opnc\Desktop\meeting_notes\meeting_notes`. SSH key access as `opnc` works from the dev box.
- Containers: `caption-hub` (:8085) and `caption-whisperlive` (internal only). Host ports 9000 and 9090
  are taken by other containers (`fde-*`, minio), hence no WhisperLive port mapping.
- **Ollama is the host's own** (`host.docker.internal:11434`, Ollama 0.40, many models incl. `qwen3.5:9b`),
  not a compose service. Odoo 18 runs on the same host (`odoo-test-instance`, :8069 →
  `ODOO_URL=http://host.docker.internal:8069`).
- GPU memory is shared with LM Studio and ComfyUI on that host; when they hold a model, Ollama fails to
  load anything (`cudaMalloc failed`) and finalize falls back to the transcript.
- Image rebuilds must run in an interactive terminal on the host (credential helper); `docker compose up
  -d --no-build` works over SSH. The hub mounts the Docker socket only to restart WhisperLive.
- Dev box `192.168.10.25` (no usable GPU): everything runs with `TRANSCRIBER=mock`; Python 3.14 in
  `.venv`, Python 3.12 (Astral) in `client\.venv312` for the exe build.
- Speakers used in scripts/tests: Muhammad, Gabriele, Ginadomenico (real colleagues' first names).

## Open items

- Finalize with the real LLM and Odoo publishing have not been exercised end to end on the host
  (blocked on free VRAM and on Odoo credentials in the dashboard).
- Loopback (`Remote-Call`) capture is unit-tested with a fake stream only.
- The dashboard restart-WhisperLive path is tested with a fake Docker API; not yet exercised on the host.
