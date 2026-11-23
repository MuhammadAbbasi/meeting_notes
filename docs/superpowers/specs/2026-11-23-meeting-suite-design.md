# Office Meeting Transcription, Live Captions & Odoo Minutes — Design

Date: 2026-11-23. Source request: `master_prompt.txt` + `odoo_integration_idea.txt` (same folder).

## Intent

Colleagues at separate Windows workstations (and sometimes in the same room) run calls in
any browser or desktop app. One shared GPU host transcribes everyone live, every workstation
sees the captions of everyone in its meeting, and at the end an LLM writes consolidated
minutes that land in Odoo 18 as a parent task with one assigned sub-task per action item.
Everything self-hosted on the LAN.

## Goals / non-goals / anti-goals

- Goals: live captions across workstations in under ~3 s; speaker = workstation user (no
  diarization needed); same-room mic bleed does not duplicate sentences; minutes + Odoo push
  with one click; zero-dependency `.exe` for colleagues.
- Non-goals: cloud services; speaker diarization inside one stream; mobile clients; auth
  beyond the LAN boundary (M1–M4).
- Anti-goals: a client that must know the WhisperLive protocol; four parallel agents
  guessing shared contracts.

## Constraints

- GPU host `192.168.10.17`: Win11 + WSL2 Docker, RTX 4060 Ti 16 GB. Dev machine `.25` has a
  GTX 1650 4 GB and Docker Desktop stopped: development runs against a mock transcriber.
- Workstations: Windows, Chrome/Firefox/Edge/Brave, any call app. Client Python 3.11/3.12
  for audio wheels (dev box has 3.14; hub code must run on 3.11+).
- WhisperLive `ghcr.io/collabora/whisperlive-gpu:latest`. Verified from source
  (2026-11-23): custom model must be a local path or a HF id containing `/`;
  `--batch_inference` forces single-model mode; `--max_connection_time` default 300 s.
- Odoo 18 on-prem via XML-RPC with API key (`odoo_integration_idea.txt` is the reference
  implementation).

## Approaches considered

1. **Four parallel agents, one file set each (the master prompt).** Fast to launch, but the
   client, hub and tests share two wire contracts nobody owns; integration happens at the
   end by "fixing discrepancies". Rejected.
2. **Direct: client ↔ WhisperLive + client ↔ hub.** Hub stays light, but every `.exe`
   embeds the WhisperLive protocol, dedup only sees arrival time (seconds of jitter), and
   nothing is testable without a Windows mic. Rejected.
3. **Hub proxy (chosen).** Client sends raw PCM + a name to the hub only. Hub owns the
   WhisperLive connections, the audio-time clock, RMS, dedup, persistence, LLM, Odoo.
   WhisperLive is mockable; the client is thin. Cost: ~64 kB/s per stream through the hub.

## Decided design

- **Wire contracts** are fixed in `CONTRACTS.md` before any code and are the only coupling
  between `client/`, `server/` and `tests/`.
- **Audio-time clock:** for each stream the hub records `stream_start_ts` (wall-clock at
  first PCM frame) and counts samples; a segment's absolute time is
  `stream_start_ts + segment.start`. Dedup (M2) compares absolute times within a 2.0 s
  window and keeps the higher mean-RMS stream.
- **Transcriber abstraction:** `Transcriber` with `start/feed/close` and a callback;
  `MockTranscriber` (deterministic, scripted) and `WhisperLiveTranscriber`; chosen by env
  `TRANSCRIBER=mock|whisperlive`.
- **Model:** `WHISPER_MODEL=deepdml/faster-whisper-large-v3-turbo-ct2` by default (env),
  `--batch_inference --max_clients 10 --max_connection_time 14400`.
- **Persistence:** `data/meetings/<meeting_id>.jsonl`, append-only completed segments.
- **Loopback audio** is a second stream from the same client with speaker `Remote-Call`.
- **Odoo:** parent `project.task` + sub-tasks with `user_ids`/`parent_id`, speaker→user
  resolution as in `odoo_integration_idea.txt`; offline/unconfigured → Markdown saved
  locally, no error.

## Milestones

- **M1** Hub proxy + mock transcriber + WhisperLive adapter + web viewer + thin Windows
  client (mic + overlay) + simulator. Live captions across 2 PCs.
- **M2** Cross-talk dedup on audio-time + RMS, with unit tests from recorded bleed cases.
- **M3** Ollama minutes (`/finalize`) + Odoo publish + overlay "Finalize" button.
- **M4** Loopback (`PyAudioWPatch`), tray icon, first-run dialog, PyInstaller, README.

## Left to the builder

Overlay look & feel within the stated controls; exact backoff caps; viewer styling;
chunk size (256 ms default) as long as the contract's frame rules hold.
