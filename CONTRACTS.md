# Wire Contracts

These are the only agreements between `client/`, `server/` and `tests/`. Change them here
first, then the code.

## 1. Client ↔ Hub

### 1.1 Speaker stream

`ws://<HUB_HOST>:8085/ws/{meeting_id}/{speaker}?lang=<auto|it|en|...>`

- `meeting_id`: `^[A-Za-z0-9_-]{1,64}$`. `speaker`: `^[A-Za-z0-9 _.-]{1,40}$` (URL-encoded).
  Anything else: the hub closes the WebSocket with code 1008 right after accepting it.
- `lang` default `auto` (WhisperLive language detection). Loopback audio is a second
  connection from the same client with `speaker=Remote-Call`.
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
    partial for that speaker; a `completed:true` caption is emitted exactly once.
  - `{"type":"error","message":str}` then close.

### 1.2 Listener (viewer) stream

`ws://<HUB_HOST>:8085/listen/{meeting_id}` – receives `roster` and `caption` frames,
sends nothing.

### 1.3 HTTP

- `GET /health` → `{"status":"ok","transcriber":"mock"|"whisperlive","rooms":int}`
- `GET /captions/{meeting_id}` → HTML viewer (live + history).
- `GET /api/meetings/{meeting_id}/segments` → JSON list of persisted completed segments,
  each `{"speaker","text","start","end","ts","rms"}` in file order.
- (M3) `POST /api/meetings/{meeting_id}/finalize` → `{"markdown":str,"odoo":{...}|null}`.

### 1.4 Persistence

`data/meetings/{meeting_id}.jsonl` – one JSON object per completed caption (same fields
as 1.3), append-only.

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
   - `{"uid","status":"WAIT","message":<minutes float>}` – server full; hub reports
     `error` to the client (M1).
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
