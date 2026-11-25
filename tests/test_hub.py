import importlib
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server.hub
from server.transcriber import TranscriberUnavailable


def make_client(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("TRANSCRIBER", "mock")
    monkeypatch.delenv("MOCK_SCRIPT", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEDUP_HOLD_S", "0")
    monkeypatch.setenv("NOISE_FLOOR_RMS", "0")  # most tests send digital silence; the noise test sets its own
    monkeypatch.setenv("SAME_ROOM_RATIO", "0")  # each echo filter is tested on its own; the loudness test sets it
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("DIST_DIR", str(tmp_path / "dist"))
    monkeypatch.setenv("WHISPER_MODEL_FILE", str(tmp_path / "config" / "whisper_model"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    hub = importlib.reload(server.hub)
    c = TestClient(hub.app)
    c.hub, c.tmp = hub, tmp_path
    return c


@pytest.fixture
def client(monkeypatch, tmp_path):
    with make_client(monkeypatch, tmp_path) as c:
        yield c


@pytest.fixture
def bleed_client(monkeypatch, tmp_path):
    script = tmp_path / "script.json"
    script.write_text(json.dumps({
        "Muhammad": ["Ieri il tracker T12 ha perso la comunicazione per due ore."],
        "Gabriele": ["ieri il tracker t 12 ha perso la comunicazione per due ore"],
    }), encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    with make_client(monkeypatch, data, MOCK_SCRIPT=str(script), DEDUP_HOLD_S="0.3") as c:
        yield c


def pcm(seconds, amplitude=0.0):
    return np.full(int(16000 * seconds), amplitude, dtype=np.float32).tobytes()


def next_of(ws, kind, **match):
    """Receive frames until one of type `kind` matching all `match` fields arrives."""
    for _ in range(50):
        msg = ws.receive_json()
        if msg["type"] == kind and all(msg.get(k) == v for k, v in match.items()):
            return msg
    raise AssertionError(f"no {kind} {match} frame received")


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["transcriber"] == "mock"


def test_speaker_gets_ready_then_roster(client):
    with client.websocket_connect("/ws/daily/Muhammad") as ws:
        assert ws.receive_json() == {
            "type": "ready", "meeting_id": "daily", "speaker": "Muhammad", "transcriber": "mock"}
        assert ws.receive_json() == {"type": "roster", "meeting_id": "daily", "speakers": ["Muhammad"]}


def test_caption_broadcast_to_listener_and_other_speaker(client):
    with client.websocket_connect("/ws/daily/Muhammad") as m, \
         client.websocket_connect("/ws/daily/Gabriele") as g, \
         client.websocket_connect("/listen/daily") as lst:
        assert next_of(lst, "roster")["speakers"] == ["Muhammad", "Gabriele"]
        for _ in range(8):
            m.send_bytes(pcm(0.25))
        for ws in (lst, g, m):
            partial = next_of(ws, "caption", completed=False)
            assert partial["speaker"] == "Muhammad" and partial["text"] == "Muhammad "
            done = next_of(ws, "caption", completed=True)
            assert done["speaker"] == "Muhammad" and done["text"] == "Muhammad segment 1"
            assert done["start"] == 0.0 and done["end"] == 2.0 and done["rms"] == 0.0
            assert done["meeting_id"] == "daily" and done["ts"] > 1.7e9
    r = client.get("/api/meetings/daily/segments")
    assert r.status_code == 200
    assert [s["text"] for s in r.json()] == ["Muhammad segment 1"]
    assert (client.tmp / "daily.jsonl").exists()


def test_same_name_reconnect_takes_over(client):
    """A PC that reconnects after a network drop must get in at once; the stale connection is told and closed."""
    with client.websocket_connect("/ws/daily/Muhammad") as first:
        next_of(first, "roster")
        with client.websocket_connect("/ws/daily/Muhammad") as second:
            err = next_of(first, "error")
            assert err["code"] == "replaced" and "Muhammad" in err["message"]
            with pytest.raises(WebSocketDisconnect) as closed:
                while True:
                    first.receive_json()
            assert closed.value.code == 4001
            next_of(second, "ready")
            second.send_bytes(pcm(2.0))  # the old handler's cleanup must not have evicted the new one
            assert next_of(second, "caption", completed=True)["text"] == "Muhammad segment 1"
            assert client.get("/health").json()["speakers"] == 1


@pytest.mark.parametrize("path", ["/ws/x%20y/A", "/ws/daily/a%3Ab", "/listen/bad%20id"])
def test_bad_ids_rejected(client, path):
    with client.websocket_connect(path) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 1008
    assert list(client.tmp.iterdir()) == []


def test_misaligned_frame_does_not_kill_stream(client):
    with client.websocket_connect("/ws/daily/Muhammad") as ws:
        next_of(ws, "roster")
        ws.send_bytes(b"\x00\x00\x00")
        ws.send_bytes(pcm(2.0))
        assert next_of(ws, "caption", completed=True)["text"] == "Muhammad segment 1"


def test_end_flushes_and_closes(client):
    with client.websocket_connect("/listen/daily") as lst:
        with client.websocket_connect("/ws/daily/Muhammad") as ws:
            next_of(ws, "roster")
            ws.send_json({"type": "end"})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        assert next_of(lst, "roster", speakers=[])


def test_transcriber_start_failure_sends_error(client, monkeypatch):
    def boom(meeting_id, speaker, language, **kw):
        raise TranscriberUnavailable("WhisperLive full")

    monkeypatch.setattr(client.hub, "make_transcriber", boom)
    with client.websocket_connect("/ws/daily/Muhammad") as ws:
        err = ws.receive_json()
        assert err["type"] == "error" and "full" in err["message"]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert client.hub.rooms == {}


def test_bleed_suppressed_and_retracted(bleed_client):
    c = bleed_client
    with c.websocket_connect("/ws/room/Muhammad") as m, \
         c.websocket_connect("/ws/room/Gabriele") as g, \
         c.websocket_connect("/listen/room") as lst:
        next_of(lst, "roster", speakers=["Muhammad", "Gabriele"])
        for _ in range(8):  # same sentence, same time: Muhammad loud, Gabriele hears it as bleed
            m.send_bytes(pcm(0.25, 0.10))
            g.send_bytes(pcm(0.25, 0.01))
        kept = next_of(lst, "caption", completed=True)
        assert kept["speaker"] == "Muhammad"
        retract = next_of(lst, "retract")
        assert retract == {"type": "retract", "meeting_id": "room", "speaker": "Gabriele"}
    rows = c.get("/api/meetings/room/segments").json()
    assert [r["speaker"] for r in rows] == ["Muhammad"]


CANNED_MD = ("## Executive Summary\nok\n\n## Consolidated Discussion by Topic\n- [Muhammad] x\n\n"
             "## Key Decisions\n- y\n\n## Action Items\n\n| Owner | Task | Deadline |\n| --- | --- | --- |\n"
             "| Muhammad | Check SCADA logs | 2026-11-28 |\n")


def _record_two(client):
    with client.websocket_connect("/ws/daily/Muhammad") as ws, client.websocket_connect("/listen/daily") as lst:
        next_of(ws, "roster")
        ws.send_bytes(pcm(4.0))
        next_of(ws, "caption", completed=True)
        next_of(ws, "caption", completed=True)
        return lst


def test_finalize_without_odoo(client, monkeypatch):
    for k in ("ODOO_URL", "ODOO_DB", "ODOO_USER", "ODOO_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(client.hub, "synthesize", lambda segs, mid, url, model, *timeout: CANNED_MD)
    with client.websocket_connect("/ws/daily/Muhammad") as ws, client.websocket_connect("/listen/daily") as lst:
        next_of(ws, "roster")
        ws.send_bytes(pcm(4.0))
        next_of(ws, "caption", completed=True)
        r = client.post("/api/meetings/daily/finalize")
        assert r.status_code == 200
        body = r.json()
        assert body["llm"] is True and body["markdown"] == CANNED_MD
        assert body["odoo"]["status"] == "saved_locally" and body["saved_to"].endswith("daily.minutes.md")
        assert (client.tmp / "daily.minutes.md").read_text(encoding="utf-8") == CANNED_MD
        fin = next_of(lst, "finalized")
        assert fin == {"type": "finalized", "meeting_id": "daily", "llm": True, "odoo_status": "saved_locally"}


def test_finalize_llm_offline_falls_back(client, monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:1")
    with client.websocket_connect("/ws/daily/Gabriele") as ws:
        next_of(ws, "roster")
        ws.send_bytes(pcm(2.0))
        next_of(ws, "caption", completed=True)
    body = client.post("/api/meetings/daily/finalize").json()
    assert body["llm"] is False and "Ollama" in body["llm_error"]
    assert json.loads((client.tmp / "daily.finalize.json").read_text())["llm_error"] == body["llm_error"]
    assert body["markdown"].startswith("## Transcript") and "**[Gabriele]** Gabriele segment 1" in body["markdown"]


def test_finalize_empty_404(client):
    assert client.post("/api/meetings/nothing/finalize").status_code == 404
    assert client.post("/api/meetings/bad%20id/finalize").status_code == 400


AUTH = ("admin", "pw")


def test_admin_requires_password(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/dashboard"
    assert "Sign in" in client.get("/dashboard").text  # page is public, data is not
    r = client.get("/api/settings")
    assert r.status_code == 401 and "www-authenticate" not in r.headers  # no browser auth dialog
    assert client.get("/api/settings", auth=("x", "wrong")).status_code == 401
    assert client.put("/api/settings", json={}).status_code == 401
    assert client.get("/api/settings", auth=AUTH).status_code == 200


def test_settings_masks_secrets_and_reports_reachability(monkeypatch, tmp_path):
    with make_client(monkeypatch, tmp_path, ODOO_URL="http://127.0.0.1:1", ODOO_DB="db", ODOO_USER="u",
                     ODOO_API_KEY="supersecret", ODOO_PROJECT_ID="7",
                     SPEAKER_ALIASES='{"Ginadomenico": "Gian Domenico"}') as c:
        r = c.get("/api/settings", auth=AUTH)
        assert r.status_code == 200 and "supersecret" not in r.text
        v, st = r.json()["values"], r.json()["status"]
        assert v["odoo_url"] == "http://127.0.0.1:1" and v["odoo_api_key"] is True and v["odoo_project_id"] == 7
        assert v["speaker_aliases"] == {"Ginadomenico": "Gian Domenico"} and v["transcriber"] == "mock"
        assert st["odoo_configured"] is True and st["odoo_reachable"] is False
        assert st["llm_reachable"] is False and st["transcriber_reachable"] is True
        assert st["meetings"] == 0 and st["live_rooms"] == {} and st["agent"] is None


def test_update_settings_applies_live_and_persists(client, monkeypatch):
    restarted = []
    monkeypatch.setattr(client.hub, "restart_container", lambda name: restarted.append(name))
    r = client.put("/api/settings", auth=AUTH, json={
        "ollama_model": "qwen3.6:35b-a3b", "odoo_url": "http://127.0.0.1:1", "odoo_api_key": "k1",
        "dedup_hold_s": 0, "default_meeting": "mazara-weekly", "whisper_model": "Systran/faster-whisper-large-v3"})
    assert r.status_code == 200
    body = r.json()
    assert set(body["changed"]) == {"ollama_model", "odoo_url", "odoo_api_key", "default_meeting", "whisper_model"}
    assert body["values"]["odoo_api_key"] is True and "k1" not in r.text
    assert restarted == [] and body["notes"] == []  # mock transcriber: no WhisperLive to restart
    saved = json.loads((client.tmp / "settings.json").read_text())
    assert saved["ollama_model"] == "qwen3.6:35b-a3b" and saved["odoo_api_key"] == "k1"
    assert client.get("/download/config.json").json()["meeting_id"] == "mazara-weekly"
    assert client.put("/api/settings", auth=AUTH, json={"dedup_similarity": "high"}).status_code == 400
    assert client.put("/api/settings", auth=AUTH, json={"transcriber": "gpt"}).status_code == 400


def test_whisper_model_change_restarts_whisperlive(client, monkeypatch):
    restarted = []
    monkeypatch.setattr(client.hub, "restart_container", lambda name: restarted.append(name))
    client.put("/api/settings", auth=AUTH, json={"transcriber": "whisperlive"})
    r = client.put("/api/settings", auth=AUTH, json={"whisper_model": "Systran/faster-whisper-medium"})
    assert restarted == ["caption-whisperlive"] and "restarting" in r.json()["notes"][0]
    assert (client.tmp / "config" / "whisper_model").read_text() == "Systran/faster-whisper-medium"
    monkeypatch.setattr(client.hub, "restart_container", lambda name: (_ for _ in ()).throw(OSError("no socket")))
    r = client.post("/api/whisperlive/restart", auth=AUTH)
    assert "restart WhisperLive manually" in r.json()["message"]


def test_test_connection_endpoints(client, monkeypatch):
    r = client.post("/api/settings/test", auth=AUTH, json={"what": "odoo", "odoo_url": "http://127.0.0.1:1"})
    assert r.json()["ok"] is False and "required" in r.json()["message"]
    r = client.post("/api/settings/test", auth=AUTH, json={"what": "odoo", "odoo_url": "http://127.0.0.1:1",
                                                            "odoo_db": "d", "odoo_user": "u", "odoo_api_key": "k"})
    assert r.json()["ok"] is False and "Error" in r.json()["message"]
    monkeypatch.setattr(client.hub, "_ollama_models", lambda url, timeout=2.0: ["qwen3.5:9b", "llama3.2:latest"])
    r = client.post("/api/settings/test", auth=AUTH, json={"what": "ollama", "ollama_model": "llama3.2"})
    assert r.json()["ok"] is True and r.json()["models"] == ["qwen3.5:9b", "llama3.2:latest"]
    r = client.post("/api/settings/test", auth=AUTH, json={"what": "ollama", "ollama_model": "gemma4"})
    assert r.json()["ok"] is False and "NOT pulled" in r.json()["message"]
    assert client.post("/api/settings/test", auth=AUTH, json={"what": "nope"}).status_code == 400


def test_agent_upload_and_download(client):
    assert client.get("/download/OfficeMeetingAgent.exe").status_code == 404
    assert client.put("/api/agent/exe", auth=AUTH, content=b"tiny").status_code == 400
    blob = b"MZ" + bytes(range(256)) * 300
    r = client.put("/api/agent/exe", auth=AUTH, content=blob)
    assert r.status_code == 200 and r.json()["agent"]["size"] == len(blob)
    d = client.get("/download/OfficeMeetingAgent.exe")
    assert d.status_code == 200 and d.content == blob and "attachment" in d.headers["content-disposition"]
    cfg = client.get("/download/config.json").json()
    assert cfg["user_name"] == "" and cfg["server_ip"] == "testserver" and cfg["meeting_id"] == "daily"
    assert client.get("/api/settings", auth=AUTH).json()["status"]["agent"]["name"] == "OfficeMeetingAgent.exe"


def test_meeting_history_and_minutes(client, monkeypatch):
    monkeypatch.setattr(client.hub, "synthesize", lambda segs, mid, url, model, *timeout: CANNED_MD)
    assert client.get("/api/meetings").json() == []
    with client.websocket_connect("/ws/daily/Muhammad") as ws:
        next_of(ws, "roster")
        ws.send_bytes(pcm(4.0))
        next_of(ws, "caption", completed=True)
        assert client.get("/api/meetings").json()[0]["live"] is True
    assert client.get("/api/meetings/daily/minutes").status_code == 404
    client.post("/api/meetings/daily/finalize")
    rows = client.get("/api/meetings").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["meeting_id"] == "daily" and row["speakers"] == ["Muhammad"] and row["segments"] >= 1
    assert row["started"] <= row["ended"] and row["live"] is False
    assert row["minutes"]["llm"] is True and row["minutes"]["odoo"]["status"] == "saved_locally"
    assert client.get("/api/meetings/daily/minutes").text == CANNED_MD
    assert client.get("/api/meetings/bad%20id/minutes").status_code == 400


def test_viewer_served(client):
    r = client.get("/captions/daily")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert 'const MEETING = "daily"' in r.text and "/listen/" in r.text
    assert r.headers["cache-control"] == "no-cache" == client.get("/dashboard").headers["cache-control"]
    assert client.get("/captions/bad%20id").status_code == 400
    assert client.get("/api/meetings/bad%20id/segments").status_code == 400


def test_settings_conflict_between_two_admins(client):
    v0 = client.get("/api/settings", auth=AUTH).json()["version"]
    a = client.put("/api/settings", auth=AUTH, json={"_version": v0, "ollama_model": "gemma4"})
    assert a.status_code == 200 and a.json()["version"] == v0 + 1
    b = client.put("/api/settings", auth=AUTH, json={"_version": v0, "ollama_model": "llama3.2"})  # stale form
    assert b.status_code == 409 and "someone else" in b.json()["detail"]
    assert client.get("/api/settings", auth=AUTH).json()["values"]["ollama_model"] == "gemma4"
    bad = client.put("/api/settings", auth=AUTH, json={"ollama_model": "x", "dedup_similarity": "high"})
    assert bad.status_code == 400  # all-or-nothing: the valid field in a rejected save is not applied either
    assert client.get("/api/settings", auth=AUTH).json()["values"]["ollama_model"] == "gemma4"


def test_meeting_list_survives_noisy_files(client):
    d = client.tmp
    (d / "noisy.jsonl").write_text("\n".join([
        '{"speaker": "Muhammad", "text": "ciao a tutti", "start": 0, "end": 1, "ts": 1700000000.0, "rms": 0.1}',
        '{"speaker": "Gabriele", "text": "half a line that never fini',  # torn write
        'not json at all',
        '[1, 2, 3]',
        '{"speaker": "Ginadomenico", "text": "venerdì <b>ok</b>", "start": 1, "end": 2, "ts": "bad", "rms": 0}',
        '', '']), encoding="utf-8")
    (d / "noisy.finalize.json").write_text("{corrupt", encoding="utf-8")
    (d / "bad name!.jsonl").write_text("{}", encoding="utf-8")
    rows = client.get("/api/meetings").json()
    assert [r["meeting_id"] for r in rows] == ["noisy"]
    r = rows[0]
    assert r["segments"] == 2 and r["speakers"] == ["Ginadomenico", "Muhammad"] and r["started"] == 1700000000.0
    assert r["minutes"] is None and r["finalizing"] is False
    with (d / "noisy.jsonl").open("a", encoding="utf-8") as f:   # cache must notice new captions
        f.write('{"speaker": "Gabriele", "text": "x", "start": 2, "end": 3, "ts": 1700000005.0, "rms": 0.1}\n')
    assert client.get("/api/meetings").json()[0]["segments"] == 3


def test_finalize_runs_once_when_two_people_press(client, monkeypatch):
    import asyncio, time
    import httpx
    calls = []

    def slow_synth(segs, mid, url, model, *timeout):
        calls.append(mid)
        time.sleep(0.6)
        return CANNED_MD
    monkeypatch.setattr(client.hub, "synthesize", slow_synth)
    (client.tmp / "daily.jsonl").write_text(
        '{"speaker": "Muhammad", "text": "hello", "start": 0, "end": 1, "ts": 1.0, "rms": 0.1}\n', encoding="utf-8")

    async def both():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.hub.app), base_url="http://t") as h:
            return await asyncio.gather(h.post("/api/meetings/daily/finalize"), h.post("/api/meetings/daily/finalize"))
    a, b = asyncio.run(both())
    assert a.status_code == b.status_code == 200 and calls == ["daily"]
    assert a.json()["markdown"] == b.json()["markdown"] == CANNED_MD
    assert sum(bool(r.json().get("joined")) for r in (a, b)) == 1


def test_room_noise_is_not_captioned(monkeypatch, tmp_path):
    """An idle microphone must not fill meetings with phantom captions; real speech volume still gets through."""
    with make_client(monkeypatch, tmp_path, NOISE_FLOOR_RMS="0.005") as c:
        with c.websocket_connect("/ws/daily/Muhammad") as ws:
            next_of(ws, "roster")
            ws.send_bytes(pcm(2.0, amplitude=0.001))       # hum: dropped, and any partial is retracted
            assert next_of(ws, "retract")["speaker"] == "Muhammad"
            ws.send_bytes(pcm(2.0, amplitude=0.08))        # speech-level audio
            assert next_of(ws, "caption", completed=True)["text"] == "Muhammad segment 2"
        assert [r["text"] for r in c.get("/api/meetings/daily/segments").json()] == ["Muhammad segment 2"]


class _StallingTranscriber:
    """Real-recognizer behaviour: a partial, then nothing while the speaker is quiet; the final copy may come later."""
    kind = "fake"

    def __init__(self):
        self.fed = 0

    async def start(self, on_segments):
        self.on = on_segments

    async def feed(self, pcm):
        from server.transcriber import Segment
        self.fed += 1
        if self.fed == 1:
            await self.on([Segment(0.0, 1.5, "il tracker ha perso la comunicazione", False)])
        elif self.fed == 3:  # WhisperLive's own final copy of the same sentence, long after
            await self.on([Segment(0.0, 1.5, "il tracker ha perso la comunicazione per due ore", True)])

    async def end(self):
        pass

    async def close(self):
        pass


def test_quiet_speaker_sentence_is_finalised_once(monkeypatch, tmp_path):
    with make_client(monkeypatch, tmp_path, PARTIAL_COMMIT_S="0.5") as c:
        monkeypatch.setattr(c.hub, "make_transcriber", lambda *a, **k: _StallingTranscriber())
        with c.websocket_connect("/ws/daily/Anna") as ws:
            next_of(ws, "ready")
            ws.send_bytes(pcm(0.25))                          # partial arrives, then silence
            assert next_of(ws, "caption", completed=False)["text"] == "il tracker ha perso la comunicazione"
            done = next_of(ws, "caption", completed=True)     # made final by the hub after 0.5 s
            assert done["text"] == "il tracker ha perso la comunicazione"
            ws.send_bytes(pcm(0.25))
            ws.send_bytes(pcm(0.25))                          # recognizer's late final copy: not shown twice
            ws.send_bytes(pcm(0.25))
        rows = c.get("/api/meetings/daily/segments").json()
        assert [r["text"] for r in rows] == ["il tracker ha perso la comunicazione"]


def test_last_sentence_kept_when_speaker_leaves(monkeypatch, tmp_path):
    with make_client(monkeypatch, tmp_path, PARTIAL_COMMIT_S="60") as c:
        monkeypatch.setattr(c.hub, "make_transcriber", lambda *a, **k: _StallingTranscriber())
        with c.websocket_connect("/ws/daily/Luca") as ws:
            next_of(ws, "ready")
            ws.send_bytes(pcm(0.25))
            next_of(ws, "caption", completed=False)
        import time
        for _ in range(40):
            rows = c.get("/api/meetings/daily/segments").json()
            if rows:
                break
            time.sleep(0.05)
        assert [r["text"] for r in rows] == ["il tracker ha perso la comunicazione"]


class _RepeatingTranscriber(_StallingTranscriber):
    """After a pause the real recognizer keeps resending the same partial, sometimes with a shifted start."""

    async def feed(self, pcm):
        from server.transcriber import Segment
        self.fed += 1
        text = "the manual cleaning crews were working near live strings"
        if self.fed == 1:
            await self.on([Segment(90.8, 96.1, text, False)])
        elif self.fed in (3, 4, 5):          # same partial again after it was made final
            await self.on([Segment(90.8, 96.1, text, False)])
        elif self.fed == 6:                  # re-segmented: shifted start, same words
            await self.on([Segment(90.0, 96.1, text, True)])
        elif self.fed == 7:                  # a short phrase said twice is kept
            await self.on([Segment(100.0, 100.5, "ok", True)])
        elif self.fed == 8:
            await self.on([Segment(101.0, 101.5, "ok", True)])


def test_final_sentence_not_repeated(monkeypatch, tmp_path):
    with make_client(monkeypatch, tmp_path, PARTIAL_COMMIT_S="0.4") as c:
        monkeypatch.setattr(c.hub, "make_transcriber", lambda *a, **k: _RepeatingTranscriber())
        with c.websocket_connect("/ws/daily/Luca") as ws:
            next_of(ws, "ready")
            ws.send_bytes(pcm(0.25))
            next_of(ws, "caption", completed=True)       # finalised after the pause
            for _ in range(7):
                ws.send_bytes(pcm(0.25))
            next_of(ws, "caption", completed=True, text="ok")
            next_of(ws, "caption", completed=True, text="ok")
        texts = [r["text"] for r in c.get("/api/meetings/daily/segments").json()]
        assert texts == ["the manual cleaning crews were working near live strings", "ok", "ok"]


def test_install_script_pins_the_uploaded_build(client):
    import hashlib
    r = client.get("/install.ps1")
    assert r.status_code == 200 and "not been uploaded" in r.text and "$expected = ''" in r.text
    blob = b"MZ" + bytes(range(256)) * 300
    client.put("/api/agent/exe", auth=AUTH, content=blob)
    script = client.get("/install.ps1").text
    sha = hashlib.sha256(blob).hexdigest().upper()
    assert f"$expected = '{sha}'" in script and "$base = 'http://testserver'" in script
    assert "Get-FileHash" in script and "Startup" in script and "LOCALAPPDATA" in script
    assert client.get("/api/settings", auth=AUTH).json()["status"]["agent"]["sha256"] == sha
    assert "removed" in client.get("/uninstall.ps1").text
    assert client.get("/icon.png").headers["content-type"] == "image/png"


def test_same_room_echo_dropped_but_simultaneous_speakers_kept(monkeypatch, tmp_path):
    """Two mics in one room: Gabriele's mic hears Muhammad 10x quieter (different words, so text dedup can't
    help). The echo must go; when both actually talk at normal volume, both are kept."""
    script = tmp_path / "s.json"
    script.write_text(json.dumps({"Muhammad": ["il tracker ha perso la comunicazione ieri sera"],
                                  "Gabriele": ["the inverter telemetry looks fine this morning"]}), encoding="utf-8")
    data = tmp_path / "d"
    data.mkdir()
    with make_client(monkeypatch, data, MOCK_SCRIPT=str(script), SAME_ROOM_RATIO="5") as c:
        with c.websocket_connect("/ws/room/Muhammad") as a, c.websocket_connect("/ws/room/Gabriele") as b:
            next_of(a, "ready"); next_of(b, "ready")
            a.send_bytes(pcm(1.0, 0.2)); b.send_bytes(pcm(1.0, 0.02))      # Muhammad speaks, Gabriele's mic: echo
            a.send_bytes(pcm(1.0, 0.2)); b.send_bytes(pcm(1.0, 0.02))
            next_of(a, "caption", completed=True, speaker="Muhammad")
            next_of(a, "retract", speaker="Gabriele")
        assert [r["speaker"] for r in c.get("/api/meetings/room/segments").json()] == ["Muhammad"]
        with c.websocket_connect("/ws/both/Muhammad") as a, c.websocket_connect("/ws/both/Gabriele") as b:
            next_of(a, "ready"); next_of(b, "ready")
            for _ in range(2):
                a.send_bytes(pcm(1.0, 0.2)); b.send_bytes(pcm(1.0, 0.15))  # both really talking
            next_of(a, "caption", completed=True, speaker="Muhammad")
            next_of(a, "caption", completed=True, speaker="Gabriele")
        assert sorted(r["speaker"] for r in c.get("/api/meetings/both/segments").json()) == ["Gabriele", "Muhammad"]
