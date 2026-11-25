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
[`docs/superpowers/specs/`](docs/superpowers/specs/). Working on the code (human or AI agent)?
Read [`AGENTS.md`](AGENTS.md) first.

**Status (2026-11-24):** deployed on the office GPU host; live transcription of real speech
(Italian and English) verified end to end through the hub. Dashboard, settings, agent download
and minutes (transcript fallback) verified. LLM minutes need free GPU memory on the host (see
Troubleshooting); Odoo publishing is implemented and unit-tested but not yet exercised against
the office Odoo.

## 1. Server (GPU host, e.g. `192.168.10.17`)

Requirements: Docker with the NVIDIA container toolkit (Docker Desktop + WSL2 on Windows is fine),
an NVIDIA GPU with ≥ 8 GB VRAM (16 GB recommended for 10 concurrent streams + the LLM).

```powershell
git clone https://github.com/MuhammadAbbasi/meeting_notes.git
cd meeting_notes
copy .env.example .env        # edit: WHISPER_MODEL, OLLAMA_MODEL, ODOO_* (Odoo is optional)
ollama list                   # OLLAMA_MODEL must be one of these; else: ollama pull qwen3.5:9b
docker compose up -d --build
docker compose logs -f whisperlive   # first start downloads the Whisper model (minutes)
curl http://localhost:8085/health    # {"status":"ok","transcriber":"whisperlive","rooms":0}
```

Compose starts only **WhisperLive** and the **hub**. Ollama is *not* started: the hub talks to the
Ollama already running on the host (`OLLAMA_URL=http://host.docker.internal:11434`), so the
models you already pulled are reused and the GPU is not loaded twice. If Odoo also runs on the
same host, use `ODOO_URL=http://host.docker.internal:8069`, never `localhost` (that would be the
hub container itself).

The stack publishes a single host port, **8085**; WhisperLive is reached only inside the compose
network (the office host already uses 9000 and 9090 for other containers). Open 8085 in the
Windows Firewall, then check `http://<hub-ip>:8085/dashboard`: all three cards should be green.

Updating: `git pull && docker compose up -d --build` in a **normal (interactive) terminal** on the
host. Over SSH, `docker build` fails with "A specified logon session does not exist" because
Docker Desktop's Windows credential helper is unavailable there; `docker compose up -d --no-build`
still works over SSH for config-only changes (`.env`, compose).

### Admin dashboard

`http://<hub-ip>:8085/dashboard` (the hub address alone redirects there). Sign in with the
`ADMIN_PASSWORD` from `.env`; it is kept for the browser session. Google Material style: Overview,
Meetings, Settings and Install the agent in a navigation drawer (a bottom bar on phones), banners for
anything that needs attention with the button that fixes it, snackbars for results. It is the place
to configure the system after the first start; `.env` only provides first-run defaults.

- **Overview** says in one sentence whether the next meeting will work, shows each service, and
  lists what needs attention (test mode on, minutes writer unreachable, Odoo not set up, no agent
  uploaded) with a direct fix.
- **Several admins at once** is safe: each save sends only the fields that person changed; edits to
  different fields merge, and only a clash on the same field asks "Keep mine / Load theirs".
  "Write minutes" runs once per meeting however many people press it, and everyone watching gets a
  snackbar when the minutes are ready. Open dashboards poll every 20 s and pause in background tabs.

- **Settings**, saved in `data/settings.json` and applied to every workstation the moment you
  click *Save* (no restart): **background noise level** (stops phantom captions such as "Thank you."
  over silence), transcriber mode and URL, **Whisper model** (saving a different one
  rewrites the model file and restarts the WhisperLive container through the Docker socket; the
  first use of a new model downloads it), **Ollama** URL and model (with the list of models
  actually present on that Ollama), **Odoo** URL / database / user / API key / project id and
  speaker aliases, cross-talk thresholds, and the default meeting id and language that new agents
  receive. Each card has a *Test* button that tries the values currently typed, before saving
  (Odoo: logs in and lists the internal users it will assign tasks to).
- **Workstation agent**: upload `OfficeMeetingAgent.exe` once (built with `client\build_exe.bat`);
  every new PC downloads it plus a pre-filled `config.json` from the same card and only has to
  type the person's name on first start.
- **Meetings**: every meeting on disk, newest first, with date, duration, speakers, caption count,
  minutes status (`LLM` / `transcript` fallback / `file` / none), Odoo result (task number or
  `local`, reason on hover), live badge for rooms with connected agents, and a link to the live
  caption page. Click a row to read the rendered **minutes** or the raw **transcript**, or to
  **Finalize** a meeting that nobody finalized.

The hub mounts `/var/run/docker.sock` only to restart WhisperLive. Keep the dashboard password
non-trivial: whoever can log in can restart that container.

`WHISPER_MODEL` must be a local path or a Hugging Face id containing a `/`
(`deepdml/faster-whisper-large-v3-turbo-ct2` by default; `Systran/faster-whisper-large-v3` for
maximum accuracy at ~3× the GPU cost). WhisperLive runs in batched single-model mode so VRAM is
shared across all streams.

Hub environment variables. Only `ADMIN_PASSWORD` matters long-term: everything else is the
first-run default that the dashboard overrides (`data/settings.json`).

| Variable | Default | Meaning |
| --- | --- | --- |
| `ADMIN_PASSWORD` | empty (dashboard disabled) | password for `/dashboard` and all settings/upload endpoints |
| `TRANSCRIBER` | `whisperlive` | `mock` for development without a GPU |
| `WHISPERLIVE_URL` | `ws://whisperlive:9090` | upstream transcriber |
| `DEDUP_HOLD_S` / `DEDUP_WINDOW_S` / `DEDUP_SIMILARITY` | `2.5` / `2.0` / `0.78` | cross-talk suppression, see CONTRACTS §1.5 |
| `OLLAMA_URL` / `OLLAMA_MODEL` | `http://host.docker.internal:11434` / `qwen3.5:9b` | minutes LLM (the host's own Ollama) |
| `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_API_KEY`, `ODOO_PROJECT_ID` | empty | leave empty to only save minutes locally |
| `SPEAKER_ALIASES` | empty | JSON, e.g. `{"Ginadomenico":"Gian Domenico Bianchi"}` to map caption names to Odoo users |
| `DATA_DIR` | `/app/data/meetings` | transcripts and minutes (mounted to `./data/meetings`) |

## 2. Workstation agent

### Option A: the `.exe` (for colleagues)

On any Windows PC with Python 3.11 or 3.12, build once:

```powershell
cd client
build_exe.bat          # -> client\dist\OfficeMeetingAgent.exe   (uses `py -3.12`, then `py -3.11`)
# other interpreter, e.g. a uv/Astral install:  set PY=py -V:Astral\CPython3.12.13 && build_exe.bat
```

Or upload from the command line instead of the dashboard form:
`curl -u admin:<ADMIN_PASSWORD> -T client\dist\OfficeMeetingAgent.exe http://<hub-ip>:8085/api/agent/exe`

Upload `client\dist\OfficeMeetingAgent.exe` once in the dashboard (*Workstation agent* card).
On each new PC open `http://<hub-ip>:8085/dashboard`, download the `.exe` and the pre-filled
`config.json` into one folder and run the `.exe`: the setup form already has the hub IP, meeting
id and language, so the person only types their name (as shown in captions). Settings are saved
next to the `.exe` in `config.json`.

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
  history for anyone without the agent (tablet, meeting-room TV): people's avatars, a larger-text
  button, a "New captions" button when you scroll back, join and leave notices, and automatic
  reconnection that reloads anything said while it was offline.
- **Same name on two PCs**: the newest connection wins (a reconnect after a Wi-Fi drop gets straight
  back in); the older PC's strip says "your name is in use on another PC" and stops retrying.

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

# many people at once: 8 noisy speakers, one bleeding mic, 30 extra caption screens (all must get every caption)
.venv\Scripts\python tests\simulate_multi_user_meeting.py --speakers Muhammad,Gabriele,Ginadomenico,Anna,Luca,Sara,Paolo,Remote-Call `
    --quiet Ginadomenico --noise 0.03 --seconds 8 --listeners 30 --expect-suppressed Ginadomenico

# messy data for the dashboard and viewer: 3-hour meeting, corrupt lines, HTML-like text, failed minutes...
.venv\Scripts\python tests\noisy_meetings.py C:\tmp\noisy ; $env:DATA_DIR="C:\tmp\noisy"   # then start the hub

# 10-person, 10-minute bilingual meeting on panel cleaning (Windows voices), then score the captions
.venv\Scripts\python tests\meeting_panel_cleaning.py C:\tmp\meeting10        # MEETING_BLEED=0 for headsets
#   -> prints the speaker and language lists; run simulate_multi_user_meeting.py with --wav <mic files> --langs <langs>
.venv\Scripts\python tests\score_meeting.py --hub http://192.168.10.17:8085 --meeting <id> --reference C:\tmp\meeting10
eference.json

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
| WhisperLive log: `Library libcublas.so.12 is not found` | the upstream image lacks CUDA 12 libs; `server/whisperlive.Dockerfile` adds them, rebuild with `docker compose up -d --build` |
| `error: WhisperLive full` | more than `--max_clients` streams; raise it in `docker-compose.yml` |
| Captions appear 2–3 s after the sentence ends | expected: `DEDUP_HOLD_S` hold for bleed suppression; set `0` to disable |
| Two people's identical sentence both kept | they were > 2 s apart or < 4 words; tune `DEDUP_WINDOW_S`, `DEDUP_SIMILARITY` |
| Finalize says `transcript` instead of `LLM` | Ollama down, model not pulled, or **GPU memory full**: the hub log shows the Ollama error. On the office host, LM Studio / ComfyUI holding a model leaves Ollama with `cudaMalloc failed: out of memory`; unload them (a 9B model needs ~6 GB free) or pick a smaller model in the dashboard. `ollama list` on the host shows what is pulled |
| Finalize says `saved_locally` | Odoo not configured or unreachable; see hub log, file is in `data/meetings/`. The dashboard shows the reason on hover of the red `local` tag |
| Dashboard card red | the hub cannot open a TCP connection to that URL: container down, wrong URL, or firewall |
| Italian captions come out in English, or one person gets no captions, when several people talk | measured on the office server: automatic language detection mixes languages under load, and with the graphics card shared with LM Studio/ComfyUI a third simultaneous speaker loses most captions. Set each person's language (not *auto*) and free GPU memory |
| Final captions appear about 9 s after a sentence | measured with one speaker too: WhisperLive marks a sentence final only once its text is stable, plus the 2.5 s same-room check. The grey in-progress text shows much sooner |
| Captions show things nobody said ("Thank you.", "감사합니다", "Bye.") | Whisper inventing words over room noise: raise *Background noise level* in Settings (on real data 0.005 removed 58% of them and 0.008 removed 69%, with no real speech lost) |
| Strip says "your name is in use on another PC" | two PCs joined the same meeting with the same name; change `user_name` in one `config.json` |
| A stopped agent still sends audio | the one-file exe runs as two processes; quit from the tray icon, or end both `OfficeMeetingAgent.exe` processes in Task Manager |
| Dashboard login form says `admin disabled` | `ADMIN_PASSWORD` missing in `.env`; add it and `docker compose up -d` |
| `/download/OfficeMeetingAgent.exe` → `no agent uploaded yet` | nobody has built and uploaded the exe yet: section 2, option A |
| WhisperLive log: `Unexpected error: <ServerConnection ...>` every 20 s | harmless: the dashboard's reachability probe opens and closes a TCP connection |
| System audio not captured | `pip install pyaudiowpatch` (Python ≤ 3.12), default playback device must be a WASAPI device |
