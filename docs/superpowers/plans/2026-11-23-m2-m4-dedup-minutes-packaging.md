# M2–M4: Cross-Talk Dedup, Minutes + Odoo, Packaging — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Same-room mic bleed never duplicates a sentence; one click turns a meeting into Markdown minutes in Odoo 18 (parent task + assigned sub-tasks) with a local fallback; the client ships as one `.exe` with loopback capture and a first-run dialog.

**Architecture:** Completed captions pass through a per-room `CrossTalkFilter` with a short hold window; duplicates (text similarity ≥ 0.78, absolute audio time within 2.0 s, other speaker) keep the louder stream and the loser is retracted. `/finalize` loads the JSONL, asks Ollama (stdlib HTTP) for structured Markdown, falls back to a plain transcript if Ollama is down, saves `.minutes.md`, and publishes to Odoo via XML-RPC when configured. Client gains a loopback stream (`Remote-Call`), setup dialog, meeting switcher, PyInstaller script.

**Tech Stack:** stdlib `difflib`, `urllib`, `xmlrpc.client`; FastAPI; tkinter; `PyAudioWPatch` (loopback); PyInstaller.

**Spec:** `docs/superpowers/specs/2026-11-23-meeting-suite-design.md`, `CONTRACTS.md` (updated by Task 1 and Task 4), `odoo_integration_idea.txt` (reference Odoo client).

## Global Constraints

- Env (hub): `DEDUP_HOLD_S` (default `2.5`, `0` = decide immediately), `DEDUP_WINDOW_S=2.0`, `DEDUP_SIMILARITY=0.78`, `OLLAMA_URL` (default `http://ollama:11434`), `OLLAMA_MODEL` (default `qwen3.5:9b`), `ODOO_URL/ODOO_DB/ODOO_USER/ODOO_API_KEY/ODOO_PROJECT_ID`, `SPEAKER_ALIASES` (JSON object string, optional).
- No new hub runtime dependency. Client adds `PyAudioWPatch` (optional import) and PyInstaller (build-time only).
- Python 3.11 syntax floor.

## Review Focus

1. Two speakers say the *same* short phrase legitimately ("ok", "sì") → must not be suppressed: similarity applies only to texts with ≥ 4 words (Task 1 `test_short_phrases_never_suppressed`).
2. Ollama answers with `<think>…</think>` and the Markdown fenced in ```` ```markdown ```` → stripped before saving/parsing (Task 2 `test_clean_response_strips_think_and_fences`).
3. Action-items table with bold owners, missing deadline column, `DD/MM/YYYY` and separator rows with spaces → all rows parsed, bad dates dropped not crashed (Task 3 `test_parse_noisy_action_table`).
4. Odoo unreachable / credentials missing → finalize still returns Markdown and saves the file; response says `"odoo": {"status": "saved_locally"}` (Task 4 `test_finalize_without_odoo`).
5. Loopback device missing (no WASAPI loopback) → agent logs and continues mic-only, never crashes (Task 5 `test_loopback_missing_device_is_noop`).

---

### Task 1: Cross-talk filter (`server/deduplicator.py`) + hub hold/retract

**Files:** Create `server/deduplicator.py`, `tests/test_deduplicator.py`; Modify `server/hub.py`, `server/meeting.py`, `server/static/captions.html`, `client/overlay_ui.py`, `CONTRACTS.md`, `tests/test_hub.py`.

**Interfaces:**
- `normalize_text(s: str) -> str` (lowercase, strip punctuation, collapse spaces).
- `text_similarity(a: str, b: str) -> float` – `SequenceMatcher.ratio()` on normalized texts; `1.0` if the shorter (≥ 4 words) is contained in the longer.
- `class CrossTalkFilter(window_s=2.0, similarity=0.78, keep_s=20.0)`: `add(frame: dict) -> None` (register pending), `should_emit(frame) -> bool` (False iff another speaker has a registered frame with `|ts - other.ts| <= window_s`, similarity ≥ threshold, both ≥ 4 words, and `other.rms > frame.rms` or equal RMS with earlier arrival), `prune(now)`.
- CONTRACTS §1.1 adds `{"type":"retract","meeting_id":str,"speaker":str}` → clients clear that speaker's partial.
- Hub: `on_segments` completed → `filter.add(frame)`, `asyncio.create_task(_flush(room, frame))`: sleep `DEDUP_HOLD_S`, emit or retract.

- [ ] Tests: `test_similarity_handles_stt_noise` ("il tracker T12 ha perso la comunicazione per due ore" vs "il tracker t 12 ha perso la comunicazione, per due ore." ≥ 0.9), `test_containment_counts_as_similar`, `test_short_phrases_never_suppressed`, `test_quieter_duplicate_suppressed_either_order`, `test_outside_window_not_suppressed`, `test_different_text_not_suppressed`, `test_same_speaker_never_suppressed`, hub: `test_bleed_suppressed_and_retracted` (two mock speakers, same script line, hold 0.2 s, quieter one gets `retract`, 1 persisted).
- [ ] Implement, run, commit `feat(server): cross-talk dedup with hold window and retract`.

### Task 2: Ollama synthesizer (`server/ollama_synthesizer.py`)

**Interfaces:**
- `transcript_markdown(segments: list[dict]) -> str` – fallback: `## Transcript` with `**[Speaker]** text` lines.
- `build_messages(segments, meeting_id) -> list[dict]` – system + user prompt requiring sections `## Executive Summary`, `## Consolidated Discussion by Topic`, `## Key Decisions`, `## Action Items` (table Owner | Task | Deadline), `[Speaker]` attributions, keep minority views.
- `clean_response(text) -> str` – drop `<think>…</think>`, unwrap ``` fences, strip.
- `synthesize(segments, meeting_id, url, model, timeout=300) -> str` – POST `/api/chat` `{"model","messages","stream":false,"options":{"temperature":0.2}}` via `urllib`; raises `SynthesisUnavailable` on any error.

- [ ] Tests (monkeypatched `urllib.request.urlopen`): `test_build_messages_contains_sections_and_speakers`, `test_clean_response_strips_think_and_fences`, `test_synthesize_posts_expected_payload`, `test_synthesize_raises_when_offline`, `test_transcript_markdown_fallback`.
- [ ] Implement, run, commit `feat(server): Ollama minutes synthesizer`.

### Task 3: Odoo client (`server/odoo_client.py`)

Port `odoo_integration_idea.txt` as-is (class `OdooMeetingClient`), plus `publish_or_save(meeting_id, markdown, participants, out_dir) -> dict` (`{"status":"published",...}` or `{"status":"saved_locally","path":...,"reason":...}`); aliases from `SPEAKER_ALIASES`.

- [ ] Tests with a fake `_execute` (records calls, returns canned users/ids): `test_parse_noisy_action_table`, `test_normalize_deadline_formats`, `test_markdown_to_html_lists_and_bold`, `test_resolve_alias_exact_firstname_fuzzy_unresolved`, `test_publish_creates_parent_and_assigned_subtasks`, `test_publish_or_save_when_unconfigured`.
- [ ] Implement, run, commit `feat(server): Odoo 18 client with sub-tasks and local fallback`.

### Task 4: Finalize endpoint + client button

- `POST /api/meetings/{id}/finalize` → 404 if no segments; else `{"meeting_id","markdown","llm":bool,"saved_to":str,"odoo":dict}`; writes `data/meetings/<id>.minutes.md`; broadcasts `{"type":"finalized","meeting_id","llm":bool,"odoo_status":str}` to the room. CONTRACTS §1.3 updated.
- Overlay button **Finalize** and tray item **Finalize & push to Odoo** → `hub_link.finalize(http_url, meeting_id) -> dict` in a thread; status shows result.
- [ ] Tests: `test_finalize_without_odoo` (Ollama monkeypatched to return canned Markdown; odoo unconfigured → saved_locally, file exists), `test_finalize_llm_offline_falls_back` (`llm: false`, transcript markdown), `test_finalize_empty_404`.
- [ ] Implement, run, commit `feat: finalize endpoint, minutes file, overlay/tray finalize`.

### Task 5: Loopback, first-run dialog, meeting switcher, exe

- `client/audio_capture.py`: `LoopbackCapture(blocksize=4096, _stream_factory=None)` using `pyaudiowpatch` default WASAPI loopback device, resampled to 16 kHz mono float32 with numpy; `start()` returns False (and logs) if the module or device is missing.
- `client/setup_dialog.py`: `ask_config(defaults: dict) -> dict | None` tkinter form (user_name, server_ip, meeting_id, language, capture_system_audio).
- `office_agent.py`: no config → dialog instead of exit; `capture_system_audio` → second `HubLink` with speaker `Remote-Call`; tray **Change meeting…** → `ask_meeting()` → stop/start.
- `client/build_exe.bat`: `pyinstaller --onefile --windowed --name OfficeMeetingAgent --add-data "config.example.json;." office_agent.py`.
- [ ] Tests: `test_loopback_missing_device_is_noop`, `test_loopback_resamples_stereo_48k_to_mono_16k` (fake stream delivering int16 stereo 48 kHz → 4096 float32 mono samples per read).
- [ ] Implement, run, commit `feat(client): loopback stream, setup dialog, meeting switcher, exe build`.

### Task 6: README + noisy end-to-end

- `README.md`: what it is, architecture diagram (text), server start on .17, model pull, client install/exe, first run, tray usage, finalize, Odoo setup, troubleshooting, testing (mock, simulator, bleed simulation, WAV + `--noise`).
- Simulator: `--quiet NAME` (amplitude 0.03 for that speaker), `--noise 0.02` (Gaussian noise added to all audio), `--expect-suppressed NAME` (fails if that speaker has persisted segments), `--finalize` (calls finalize and prints the Markdown head). `tests/mock_script_bleed.json`: Ginadomenico's lines are noisy copies of Muhammad's.
- [ ] Verify: bleed simulation → suppressed speaker has 0 persisted, exit 0; finalize with Ollama offline → fallback Markdown saved.
- [ ] Commit `docs: README; test: noisy/bleed simulation`, push.
