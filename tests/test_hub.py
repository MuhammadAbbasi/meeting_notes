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


def test_duplicate_speaker_refused(client):
    with client.websocket_connect("/ws/daily/Muhammad") as first:
        next_of(first, "roster")
        with client.websocket_connect("/ws/daily/Muhammad") as second:
            err = second.receive_json()
            assert err["type"] == "error" and "Muhammad" in err["message"]
            with pytest.raises(WebSocketDisconnect):
                second.receive_json()
        first.send_bytes(pcm(2.0))
        assert next_of(first, "caption", completed=True)["text"] == "Muhammad segment 1"


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
    monkeypatch.setattr(client.hub, "synthesize", lambda segs, mid, url, model: CANNED_MD)
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
    assert "Log in" in client.get("/dashboard").text  # page is public, data is not
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
    monkeypatch.setattr(client.hub, "synthesize", lambda segs, mid, url, model: CANNED_MD)
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
    assert "/listen/daily" in r.text
    assert client.get("/captions/bad%20id").status_code == 400
    assert client.get("/api/meetings/bad%20id/segments").status_code == 400
