# Office Meeting Captions & Minutes

Self-hosted, LAN-only meeting intelligence for a small office:

- every colleague runs a tiny Windows agent that streams their microphone (and optionally what their
  PC plays, i.e. the remote side of a call) to one central hub;
- one shared GPU box transcribes everybody live with [WhisperLive](https://github.com/collabora/WhisperLive);
- everyone in the same meeting sees everyone's captions in a small always-on-top strip or a browser page;
- same-room mic bleed (two PCs hearing the same sentence) is suppressed automatically;
- one click turns the meeting into Markdown minutes (local Ollama LLM) and creates an **Odoo 18**
  task with the minutes plus one assigned sub-task per action item. If Odoo or Ollama are down,
  the minutes are still saved as a file.

Nothing leaves the LAN. Works with any browser or call app because audio is captured at OS level.

```
 workstation A ──mic PCM──┐                       ┌── WhisperLive (GPU, :9090)
 workstation B ──mic PCM──┼─► caption-hub (:8085) ─┼── Ollama (GPU, :11434)
 browser viewer ◄─captions┘   │  dedup, JSONL      └── Odoo 18 (XML-RPC)
                              └─ data/meetings/<id>.jsonl, <id>.minutes.md
```

The wire protocols are documented in [`CONTRACTS.md`](CONTRACTS.md); the design in
[`docs/superpowers/specs/`](docs/superpowers/specs/).

## 1. Server (GPU host, e.g. `192.168.10.17`)

Requirements: Docker with the NVIDIA container toolkit (Docker Desktop + WSL2 on Windows is fine),
an NVIDIA GPU with ≥ 8 GB VRAM (16 GB recommended for 10 concurrent streams + the LLM).

```powershell
git clone https://github.com/MuhammadAbbasi/meeting_notes.git
cd meeting_notes
copy .env.example .env        # edit: WHISPER_MODEL, OLLAMA_MODEL, ODOO_* (Odoo is optional)
docker compose up -d --build
docker compose logs -f whisperlive   # first start downloads the model (minutes)
docker compose exec ollama ollama pull qwen3.5:9b   # or whatever OLLAMA_MODEL you set
curl http://localhost:8085/health    # {"status":"ok","transcriber":"whisperlive","rooms":0}
```

Open firewall ports **8085** (hub) on the host; 9090/11434 only need to be reachable inside Docker.

`WHISPER_MODEL` must be a local path or a Hugging Face id containing a `/`
(`deepdml/faster-whisper-large-v3-turbo-ct2` by default; `Systran/faster-whisper-large-v3` for
maximum accuracy at ~3× the GPU cost). WhisperLive runs in batched single-model mode so VRAM is
shared across all streams.

Hub environment variables (all optional):

| Variable | Default | Meaning |
| --- | --- | --- |
| `TRANSCRIBER` | `whisperlive` | `mock` for development without a GPU |
| `WHISPERLIVE_URL` | `ws://whisperlive:9090` | upstream transcriber |
| `DEDUP_HOLD_S` / `DEDUP_WINDOW_S` / `DEDUP_SIMILARITY` | `2.5` / `2.0` / `0.78` | cross-talk suppression, see CONTRACTS §1.5 |
| `OLLAMA_URL` / `OLLAMA_MODEL` | `http://ollama:11434` / `qwen3.5:9b` | minutes LLM |
| `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_API_KEY`, `ODOO_PROJECT_ID` | empty | leave empty to only save minutes locally |
| `SPEAKER_ALIASES` | empty | JSON, e.g. `{"Ginadomenico":"Gian Domenico Bianchi"}` to map caption names to Odoo users |
| `DATA_DIR` | `/app/data/meetings` | transcripts and minutes (mounted to `./data/meetings`) |

## 2. Workstation agent

### Option A: the `.exe` (for colleagues)

On any Windows PC with Python 3.11 or 3.12, build once:

```powershell
cd client
build_exe.bat          # -> client\dist\OfficeMeetingAgent.exe
```

Copy `OfficeMeetingAgent.exe` to each colleague. The first start opens a setup form: name
(as shown in captions), hub IP, meeting id, language (`auto`, `it`, `en`…), and whether to also
capture system audio. Settings are saved next to the `.exe` in `config.json`.

### Option B: from source

```powershell
pip install -r client\requirements.txt
python client\office_agent.py
```

### Using it

- **Caption strip**: bottom-right, half transparent, drag it anywhere. Buttons: *Finalize*,
  *Pause*, *Hide*. Completed sentences are crisp; in-progress speech is grey italic.
- **Tray icon** (speech bubble; green = streaming, yellow = paused, grey = stopped, red = reconnecting):
  Show/Hide captions (also double-click), Start/Stop streaming, Pause/Resume (sends silence, keeps
  the connection), Finalize & push to Odoo, Change meeting…, Quit.
- **Meeting id** groups people: everyone who enters the same id sees each other's captions.
  Use e.g. `daily`, `mazara-weekly`, `2026-11-23-om`. Letters, digits, `-`, `_` only.
- **System audio** (`capture_system_audio`): what your PC plays is streamed as a second speaker
  called `Remote-Call`, so remote participants in any browser/app are captured. Only one person
  in the room should enable it, or the remote side will be heard twice.
- **Browser viewer**: `http://<hub-ip>:8085/captions/<meeting-id>` shows live captions and the
  history for anyone without the agent (tablet, meeting-room PC).

### Finalize

Click *Finalize* (strip or tray) at the end. The hub reads the meeting's transcript, asks Ollama
for minutes with sections *Executive Summary*, *Consolidated Discussion by Topic*, *Key Decisions*
and an *Action Items* table, saves `data/meetings/<id>.minutes.md`, and, if Odoo is configured,
creates `[Meeting Notes] <id> (<date>)` as a `project.task` assigned to all participants plus one
sub-task per action item assigned to the owner (name matched to Odoo users exactly, by first
name, by alias, or fuzzily). The LLM step can take a few minutes on a 9B model; the status line
tells you when it is done. If Ollama is unreachable the file contains the plain transcript.

## 3. Development and testing (no GPU needed)

```powershell
python -m venv .venv ; .venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q                        # unit + integration tests

# hub with the deterministic mock transcriber (scripted lines every 2 s of audio)
$env:TRANSCRIBER="mock"; $env:MOCK_SCRIPT="tests\mock_script.json"
.venv\Scripts\python -m uvicorn server.hub:app --host 0.0.0.0 --port 8085

# three fake colleagues talking; ends with OK/FAIL
.venv\Scripts\python tests\simulate_multi_user_meeting.py --hub ws://127.0.0.1:8085

# cross-talk: Ginadomenico's mic hears Muhammad 10x quieter + noise -> must be suppressed, then finalize
$env:MOCK_SCRIPT="tests\mock_script_bleed.json"   # restart the hub after changing it
.venv\Scripts\python tests\simulate_multi_user_meeting.py --speakers Muhammad,Ginadomenico `
    --quiet Ginadomenico --noise 0.02 --expect-suppressed Ginadomenico --finalize

# real speech against the GPU host, with added noise
.venv\Scripts\python tests\simulate_multi_user_meeting.py --hub ws://192.168.10.17:8085 --wav meeting.wav --noise 0.02
```

Layout: `server/` hub, transcriber adapters, dedup, Ollama, Odoo · `client/` Windows agent ·
`tests/` pytest + simulator · `docs/superpowers/` spec and plans.

## 4. Troubleshooting

| Symptom | Check |
| --- | --- |
| Strip says `reconnecting (ConnectionRefusedError)` | hub not running or port 8085 blocked; `curl http://<hub-ip>:8085/health` from the workstation |
| Strip connected, no captions | you are the only speaker and silent; or WhisperLive still loading (`docker compose logs whisperlive`) |
| `error: WhisperLive full` | more than `--max_clients` streams; raise it in `docker-compose.yml` |
| Captions appear 2–3 s after the sentence ends | expected: `DEDUP_HOLD_S` hold for bleed suppression; set `0` to disable |
| Two people's identical sentence both kept | they were > 2 s apart or < 4 words; tune `DEDUP_WINDOW_S`, `DEDUP_SIMILARITY` |
| Finalize says `transcript` instead of `LLM` | Ollama down or model not pulled: `docker compose exec ollama ollama list` |
| Finalize says `saved_locally` | Odoo not configured or unreachable; see hub log, file is in `data/meetings/` |
| System audio not captured | `pip install pyaudiowpatch` (Python ≤ 3.12), default playback device must be a WASAPI device |
