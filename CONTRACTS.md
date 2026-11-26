# Wire Contracts

These are the only agreements between `client/`, `server/` and `tests/`. Change them here
first, then the code.

## 1. Client ↔ Hub

### 1.1 Speaker stream

`ws://<HUB_HOST>:8085/ws/{meeting_id}/{speaker}?lang=<auto|it|en|...>`

- `meeting_id`: `^[A-Za-z0-9_-]{1,64}$`. `speaker`: `^[A-Za-z0-9 _.-]{1,40}$` (URL-encoded).
  Anything else: the hub closes the WebSocket with code 1008 right after accepting it.
- **Order:** `ready` is sent once the transcriber accepted the stream. Other room frames (`roster`,
  `caption`, `retract`) can arrive before it when several people join at once; clients must not assume
  `ready` is the first frame.
- **Same name twice:** the newest connection wins. The previous one (usually the same PC whose
  network dropped, while the hub still held the dead socket) receives
  `{"type":"error","code":"replaced","message":str}` and is closed with code **4001**. A client
  closed with 4001 must not reconnect automatically (another PC is using that name).
- `lang` default `auto` (WhisperLive language detection). Loopback audio is a second
  connection from the same client with `speaker=Remote-Call`. If `Remote-Call` is already
  active in the room, the hub rejects a second `Remote-Call` connection with
  `{"type":"error","code":"loopback_active","message":str}` and closes with code **4002**
  (clients closed with 4002 must not reconnect automatically).
- **Client → hub, binary frames:** raw PCM, `float32` little-endian, 16 000 Hz, mono,
  values in [-1, 1]. Any frame length that is a multiple of 4 bytes. Recommended 4096
  samples (16 384 bytes, 256 ms). Silence may be sent; the hub does not VAD-gate.
- **Client → hub, text frames (JSON):**
  - `{"type":"end"}` – flush: hub sends `END_OF_AUDIO` upstream, waits ≤ 2 s for final
    segments, then closes the socket.
- **Hub → client, text frames (JSON):**
  - `{"type":"ready","meeting_id":str,"speaker":str,"transcriber":"mock"|"whisperlive"}`
    once, when the upstream transcriber accepted the stream. Audio sent before `ready` is
    buffered by the hub.
  - `{"type":"roster","meeting_id":str,"speakers":[str,...]}` to everyone in the room on
    every join/leave.
  - `{"type":"caption","meeting_id":str,"speaker":str,"text":str,"completed":bool,
     "start":float,"end":float,"ts":float,"rms":float}`
    `start`/`end`: seconds from that speaker's stream start. `ts`: epoch seconds of the
    segment start = `stream_start_ts + start`. `rms`: mean RMS of the audio in
    `[start,end]` (0.0 if unknown). A `completed:false` caption replaces the previous
    partial for that speaker; a `completed:true` caption is emitted exactly once, after a
    hold window of `DEDUP_HOLD_S` seconds (default 2.5) used for cross-talk suppression.
  - `{"type":"retract","meeting_id":str,"speaker":str}` – that speaker's pending partial was
    identified as mic bleed of another speaker; clients clear the partial. Nothing is persisted.
  - `{"type":"finalized","meeting_id":str,"llm":bool,"odoo_status":str}` – minutes were
    produced (M3).
  - `{"type":"error","message":str}` then close.

### 1.2 Listener (viewer) stream

`ws://<HUB_HOST>:8085/listen/{meeting_id}` – receives `roster` and `caption` frames,
sends nothing.

### 1.3 HTTP

- `GET /health` → `{"status":"ok","version":str,"transcriber":"mock"|"whisperlive","rooms":int,"speakers":int,"listeners":int}`
- `GET /captions/{meeting_id}` → HTML viewer (live + history). It and `/dashboard` are sent with
  `Cache-Control: no-cache`, so a redeploy reaches every open screen on its next load.
- `GET /api/meetings/{meeting_id}/segments` → JSON list of persisted completed segments,
  each `{"speaker","text","start","end","ts","rms"}` in file order.
- `POST /api/meetings/{meeting_id}/finalize` → optional JSON body
  `{"project_id"?:int|null,"attendees"?:[str,...]}`. Waits for in-flight hold-window captions
  to settle before reading segments. Returns 404 if no segments, else
  `{"meeting_id":str,"markdown":str,"llm":bool,"saved_to":str,"odoo":{"status":"published"|"saved_locally",...}}`.
  `llm:false` means Ollama was unreachable and `markdown` is the plain transcript. Minutes are
  always written to `data/meetings/{meeting_id}.minutes.md`, and the outcome to
  `data/meetings/{meeting_id}.finalize.json` (`{"finalized_at":float,"llm":bool,"llm_model":str,
  "llm_error":str|null,"odoo":{...}}`). One run per meeting at a time: a request that arrives while
  a run is going waits for it and returns the same result with `"joined": true`.
- `POST /api/meetings/{meeting_id}/publish` → 404 if `{meeting_id}.minutes.md` does not exist;
  publishes the already-saved Markdown to Odoo (updating `{meeting_id}.finalize.json`) without
  re-running Ollama, returning `{"meeting_id":str,"odoo":{"status":"published"|"saved_locally",...}}`.
- `GET /api/calendar` → `{"meetings":[{"meeting_id":str,"title":str,"start":str|null,
  "stop":str|null,"project_id":int|null,"project_name":str|null,"attendees":[str,...],
  "live_speakers":[str,...]}]}`. Lists today's/upcoming Odoo `calendar.event` entries (when
  configured and reachable) plus any live rooms; never fails if Odoo is down.
- Admin endpoints (HTTP Basic, any user, password `ADMIN_PASSWORD`; 403 when unset):
  - `GET /dashboard` → HTML admin page.
  - `GET /api/settings` → `{"values":{<key>:<value>}, "version":int, "status":{...}}`. Keys (see
    `server/settings.py` `FIELDS`): `transcriber, whisperlive_url, whisper_model, ollama_url,
    ollama_model, odoo_url, odoo_db, odoo_user, odoo_api_key, odoo_project_id, speaker_aliases,
    dedup_hold_s, dedup_window_s, dedup_similarity, noise_floor_rms, default_meeting, default_language`.
    Secrets are returned as booleans (set / not set). `status`: `transcriber_reachable,
    llm_reachable, llm_models[], llm_model_pulled, odoo_configured, odoo_reachable, data_dir,
    meetings, live_rooms{id:[speakers]}, agent{name,size,modified}|null`. Reachability is a 2 s
    TCP connect (Ollama: `GET /api/tags`); for `transcriber=mock` it is `true`.
  - `PUT /api/settings` body `{<key>:<value>,..., "_version"?:int}` → `{"changed":[keys],"values":{...},"version":int,"notes":[str]}`;
    unknown keys ignored, invalid values → 400 and nothing is applied (all-or-nothing), a secret
    sent as `""` is kept and as `null` cleared. With `_version` different from the current one →
    409 (someone saved in between). The dashboard sends only the fields the person changed, so two
    admins editing different fields merge; it asks only when both changed the same field.
    Persisted to `SETTINGS_FILE` (`data/settings.json`), which overrides the env defaults, and
    used by the hub from the next call on. A changed `whisper_model` (non-mock) is written to
    `WHISPER_MODEL_FILE` and the `WHISPERLIVE_CONTAINER` is restarted via the Docker socket.
  - `POST /api/settings/test` body `{"what":"odoo"|"ollama"|"whisperlive", <keys to try>}` →
    `{"ok":bool,"message":str,...}` using the posted values without saving them.
  - `GET /api/odoo/projects` → `{"projects":[{"id":int,"name":str}]}`.
  - `POST /api/whisperlive/restart` → `{"message":str}`.
  - `PUT /api/agent/exe` raw body = the exe → `{"agent":{...}}` (stored in `DIST_DIR`).
- `GET /download/OfficeMeetingAgent.exe` → the uploaded agent (404 before the first upload).
- `GET /install.ps1` → `text/plain` PowerShell installer bound to this hub's address and the SHA-256 of
  the uploaded build (`irm http://HUB:8085/install.ps1 | iex`); `GET /uninstall.ps1` removes it again;
  `GET /icon.png` is the app logo. `status.agent` in `/api/settings` includes `sha256`.
- `GET /download/config.json` → agent config pre-filled with this hub's host/port and the
  `default_meeting` / `default_language`, `user_name` empty (the agent asks for it on first run).
- `GET /api/meetings` → list, newest first, of
  `{"meeting_id","segments":int,"speakers":[...],"started":ts|null,"ended":ts|null,"live":bool,
  "finalizing":bool,"minutes":null|{"status":"file_only"}|<finalize.json contents>}`. Files with an
  invalid meeting name are ignored; torn or corrupt JSONL lines and a corrupt finalize record are
  skipped, never fatal. Summaries are cached per file and re-read only when a file changes.
- `GET /api/meetings/{meeting_id}/minutes` → `text/plain` Markdown of the saved minutes, 404 if not finalized.

### 1.5 Cross-talk suppression (hub-internal, affects 1.1)

Two completed captions from *different* speakers whose absolute times (`ts`) differ by at most
`DEDUP_WINDOW_S` (2.0) and whose normalized texts have similarity ≥ `DEDUP_SIMILARITY` (0.78),
both with ≥ 4 words, are the same utterance heard by two mics: the one with the higher `rms`
is emitted, the other retracted. Equal `rms` → the first to arrive wins.

**Room noise (before the above):** a caption is dropped as room noise when its `rms` is below
`noise_floor_rms` (default 0.005; 0 disables), or below 4× that and either at most 2 words or a known
Whisper silence phrase ("Thank you.", "감사합니다", "Sottotitoli creati dalla comunità Amara.org" …).
A dropped completed caption is answered with a `retract` so screens clear its partial.

**Same-room echo (before the above):** a caption is dropped (and its partial retracted) when another
microphone's loudest 256 ms chunk within ±3 s of the caption is at least `same_room_ratio` (default 5;
0 disables) times this microphone's loudest chunk there. Measured on a real 10-person one-room run:
92% of echo captions removed; with headsets nothing removed.

**Broadcast:** every frame goes to all speakers and listeners concurrently; a client that fails or
does not accept the frame within 2 s is dropped from the room, so one stalled screen never delays
the others.

### 1.4 Persistence

`data/meetings/{meeting_id}.jsonl` – one JSON object per completed caption (same fields
as 1.3), append-only.
`data/meetings/audio/{meeting_id}/{speaker}.wav` – per-speaker 16 kHz 16-bit mono PCM
recording of each stream, saved for replay or re-transcription.

## 2. Hub ↔ WhisperLive (verified against collabora/WhisperLive main, 2026-11-23)

`ws://whisperlive:9090` (plain WebSocket, no path).

1. First frame, text JSON:
   ```json
   {"uid":"<meeting>:<speaker>:<8hex>","language":null|"it"|"en","task":"transcribe",
    "model":"<WHISPER_MODEL>","use_vad":true,"send_last_n_segments":10,
    "no_speech_thresh":0.45,"clip_audio":false,"same_output_threshold":10,
    "audio_format":"float32"}
   ```
   `language:null` = auto-detect. `model` is ignored by the server in single-model mode.
2. Then binary frames: raw `float32` LE 16 kHz mono (forwarded unchanged from 1.1).
3. To finish: binary frame with bytes `END_OF_AUDIO`.
4. Server → hub text JSON, always with `"uid"`:
   - `{"uid","message":"SERVER_READY","backend":"faster_whisper"}` – start forwarding.
   - `{"uid","status":"WAIT","message":<minutes float>}` – server full; the hub retries for ~30 s
     (sessions of a finished meeting are freed late), then reports `error` "WhisperLive full" to the client.
   - `{"uid","language":"it","language_prob":0.98}` – informational.
   - `{"uid","segments":[{"start":"12.340","end":"14.120","text":" ...",
      "completed":true|false}, ...]}` – the last ≤10 *completed* segments, plus at most
      one trailing `completed:false` partial. `start`/`end` are **strings** with 3
      decimals, seconds from stream start. The hub emits a completed segment once, keyed
      by its `start` string; the trailing partial replaces the previous partial.
   - `{"uid","message":"DISCONNECT"}` – server closed the session (e.g. max time).
   - `{"uid","message":"TRANSCRIPT_RESET"}` – only after a reset request (unused).

Server flags used (docker-compose): `--backend faster_whisper
--faster_whisper_custom_model_path ${WHISPER_MODEL} --batch_inference --max_clients 10
--max_connection_time 14400 --omp_num_threads 4 --enable_rest`.
`WHISPER_MODEL` must be a local path or a Hugging Face id containing `/`.

## 3. Mock transcriber (dev/test)

`TRANSCRIBER=mock`. For every 2.0 s of audio received (32 000 samples) it emits one
completed segment `start=2k, end=2k+2` (k from 0); after the first 1.0 s of each 2 s block
it emits a partial. Text comes from `MOCK_SCRIPT` (JSON file `{speaker:[line,...]}`) in
order, else `"<speaker> segment <n>"` with `n` starting at 1. The partial text is the
first half of the line's characters.
