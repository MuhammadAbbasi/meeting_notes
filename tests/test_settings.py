import http.server
import json
import threading

import pytest

from server import docker_api
from server.settings import Settings


def test_env_defaults_then_file_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("OLLAMA_MODEL", "from-env")
    monkeypatch.setenv("ODOO_PROJECT_ID", "7")
    monkeypatch.setenv("SPEAKER_ALIASES", '{"Gina": "Gian Domenico"}')
    p = tmp_path / "settings.json"
    s = Settings(p)
    assert s.get("ollama_model") == "from-env" and s.get("odoo_project_id") == 7
    assert s.get("speaker_aliases") == {"Gina": "Gian Domenico"} and s.get("dedup_hold_s") == 2.5
    assert s.update({"ollama_model": "qwen3.6:35b-a3b", "dedup_hold_s": "1.5", "bogus": 1}) == ["ollama_model", "dedup_hold_s"]
    assert Settings(p).get("ollama_model") == "qwen3.6:35b-a3b" and Settings(p).get("dedup_hold_s") == 1.5
    assert "bogus" not in json.loads(p.read_text())


def test_secret_masked_kept_and_cleared(tmp_path):
    s = Settings(tmp_path / "s.json")
    s.update({"odoo_api_key": "topsecret"})
    assert s.public()["odoo_api_key"] is True and "topsecret" not in json.dumps(s.public())
    assert s.update({"odoo_api_key": ""}) == [] and s.get("odoo_api_key") == "topsecret"
    assert s.update({"odoo_api_key": None}) == ["odoo_api_key"] and s.get("odoo_api_key") == ""
    assert s.public()["odoo_api_key"] is False


def test_validation(tmp_path):
    s = Settings(tmp_path / "s.json")
    with pytest.raises(ValueError):
        s.update({"transcriber": "gpt"})
    with pytest.raises(ValueError):
        s.update({"dedup_similarity": "high"})
    with pytest.raises(ValueError):
        s.update({"speaker_aliases": "[1,2]"})
    assert s.update({"speaker_aliases": '{" Gina ": " G. D. "}', "odoo_project_id": ""}) == ["speaker_aliases"]
    assert s.get("speaker_aliases") == {"Gina": "G. D."} and s.get("odoo_project_id") is None


class _FakeDocker(http.server.BaseHTTPRequestHandler):
    calls: list[str] = []

    def do_POST(self):
        _FakeDocker.calls.append(self.path)
        self.send_response(204 if "caption-whisperlive" in self.path else 404)
        self.end_headers()
        if "caption-whisperlive" not in self.path:
            self.wfile.write(b'{"message":"No such container"}')

    def log_message(self, *a):
        pass


def test_restart_container_over_http():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _FakeDocker)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    conn = lambda: http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=5)  # noqa: E731
    docker_api.restart_container("caption-whisperlive", connection=conn())
    assert _FakeDocker.calls == ["/containers/caption-whisperlive/restart?t=20"]
    with pytest.raises(RuntimeError, match="No such container"):
        docker_api.restart_container("ghost", connection=conn())
    srv.shutdown()
