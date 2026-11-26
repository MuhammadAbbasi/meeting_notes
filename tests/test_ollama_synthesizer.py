import io
import json

import pytest

from server import ollama_synthesizer as syn

SEGS = [
    {"speaker": "Muhammad", "text": "Ieri il tracker T12 ha perso la comunicazione per due ore."},
    {"speaker": "Gabriele", "text": "The drop matches a grid event, not a hardware fault."},
    {"speaker": "Ginadomenico", "text": "   "},
    {"speaker": "Gabriele", "text": "I will export the VCOM data by Friday."},
]

GOOD_MD = ("## Executive Summary\nx\n\n## Consolidated Discussion by Topic\n- [Muhammad] a\n\n"
           "## Key Decisions\n- b\n\n## Action Items\n\n| Owner | Task | Deadline |\n| --- | --- | --- |\n"
           "| Gabriele | Export VCOM data | 2026-11-27 |\n")


def test_build_messages_contains_sections_and_speakers():
    msgs = syn.build_messages(SEGS, "daily")
    assert msgs[0]["role"] == "system" and all(s in msgs[0]["content"] for s in syn.SECTIONS)
    user = msgs[1]["content"]
    assert "Participants: Gabriele, Ginadomenico, Muhammad" in user
    assert "[Muhammad] Ieri il tracker T12" in user and user.count("[Gabriele]") == 2
    assert "[Ginadomenico]" not in user.split("Transcript:")[1]  # blank text dropped


def test_clean_response_strips_think_and_fences():
    noisy = "<think>\nlet me reason...\n</think>\n```markdown\n" + GOOD_MD + "```\n"
    assert syn.clean_response(noisy) == GOOD_MD
    assert syn.clean_response("  plain  ") == "plain\n"


def test_transcript_markdown_fallback():
    md = syn.transcript_markdown(SEGS)
    assert md.startswith("## Transcript") and "**[Muhammad]** Ieri" in md and md.count("**[") == 3


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_synthesize_posts_expected_payload(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"], seen["body"], seen["timeout"] = req.full_url, json.loads(req.data), timeout
        return FakeResp(json.dumps({"message": {"content": "<think>hm</think>" + GOOD_MD}}).encode())

    monkeypatch.setattr(syn.urllib.request, "urlopen", fake_urlopen)
    md = syn.synthesize(SEGS, "daily", "http://ollama:11434/", "qwen3.5:9b", timeout=42)
    assert seen["url"] == "http://ollama:11434/api/chat" and seen["timeout"] == 42
    assert seen["body"]["model"] == "qwen3.5:9b" and seen["body"]["stream"] is False
    assert seen["body"]["options"] == {"temperature": 0.2, "num_ctx": 16384}
    assert md == GOOD_MD


def test_synthesize_appends_missing_action_table(monkeypatch):
    monkeypatch.setattr(syn.urllib.request, "urlopen",
                        lambda req, timeout: FakeResp(json.dumps({"message": {"content": "## Executive Summary\nok"}}).encode()))
    md = syn.synthesize(SEGS, "daily", "http://x", "m")
    assert "## Action Items" in md and "| Owner | Task | Deadline |" in md


def test_synthesize_raises_when_offline(monkeypatch):
    def boom(req, timeout):
        raise syn.urllib.error.URLError("connection refused")

    monkeypatch.setattr(syn.urllib.request, "urlopen", boom)
    with pytest.raises(syn.SynthesisUnavailable):
        syn.synthesize(SEGS, "daily", "http://ollama:11434", "m")
    monkeypatch.setattr(syn.urllib.request, "urlopen", lambda req, timeout: FakeResp(b"not json"))
    with pytest.raises(syn.SynthesisUnavailable):
        syn.synthesize(SEGS, "daily", "http://ollama:11434", "m")


def test_synthesize_renders_structured_json(monkeypatch):
    payload = {
        "executive_summary": "Verifica telemetria inverter.",
        "topics": [{"topic": "Telemetria", "summary": "Controllo eseguito.", "speakers": ["Muhammad"]}],
        "key_decisions": ["Usare tutte le porte elettriche."],
        "action_items": [{"owner": "Muhammad", "task": "Verificare cavi", "deadline": "2026-12-01"}],
    }
    monkeypatch.setattr(syn.urllib.request, "urlopen",
                        lambda req, timeout: FakeResp(json.dumps({"message": {"content": json.dumps(payload)}}).encode()))
    md = syn.synthesize(SEGS, "daily", "http://ollama:11434", "qwen2.5:7b-instruct", language="it")
    assert "## Executive Summary\nVerifica telemetria inverter." in md
    assert "- **Telemetria**: Controllo eseguito. [Muhammad]" in md
    assert "- Usare tutte le porte elettriche." in md
    assert "| Muhammad | Verificare cavi | 2026-12-01 |" in md


def test_synthesize_chunks_long_transcript(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(json.loads(req.data))
        part = {"executive_summary": f"Part {len(calls)}.", "topics": [], "key_decisions": [],
                "action_items": [{"owner": "Gabriele", "task": f"Task {len(calls)}", "deadline": ""}]}
        return FakeResp(json.dumps({"message": {"content": json.dumps(part)}}).encode())

    monkeypatch.setattr(syn.urllib.request, "urlopen", fake_urlopen)
    big_segs = [{"speaker": "Muhammad", "text": "x" * 4000} for _ in range(5)]  # 20k chars > CHUNK_CHARS
    md = syn.synthesize(big_segs, "long", "http://ollama:11434", "m")
    assert len(calls) >= 2
    assert "Part 1. Part 2." in md and "| Gabriele | Task 1 |  |" in md and "| Gabriele | Task 2 |  |" in md


def test_clean_transcript_removes_stutters_and_fillers():
    noisy = "Ciao, ciao, e, e, e, e, sì, sì, sì, noi, noi, noi, controlliamo i cavi."
    assert syn.clean_transcript_text(noisy) == "Ciao, sì, noi, controlliamo i cavi."
    assert syn.clean_transcript_text("e, e, e, allora") == ""
