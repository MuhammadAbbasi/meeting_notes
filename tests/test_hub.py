import importlib
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server.hub
from server.transcriber import TranscriberUnavailable


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANSCRIBER", "mock")
    monkeypatch.delenv("MOCK_SCRIPT", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    hub = importlib.reload(server.hub)
    with TestClient(hub.app) as c:
        c.hub = hub
        c.tmp = tmp_path
        yield c


def pcm(seconds):
    return np.zeros(int(16000 * seconds), dtype=np.float32).tobytes()


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
    def boom(meeting_id, speaker, language):
        raise TranscriberUnavailable("WhisperLive full")

    monkeypatch.setattr(client.hub, "make_transcriber", boom)
    with client.websocket_connect("/ws/daily/Muhammad") as ws:
        err = ws.receive_json()
        assert err["type"] == "error" and "full" in err["message"]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert client.hub.rooms == {}


def test_viewer_served(client):
    r = client.get("/captions/daily")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "/listen/daily" in r.text
    assert client.get("/captions/bad%20id").status_code == 400
    assert client.get("/api/meetings/bad%20id/segments").status_code == 400
