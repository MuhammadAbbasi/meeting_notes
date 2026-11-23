"""Turns a meeting's completed captions into consolidated Markdown minutes via local Ollama."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

SECTIONS = ["## Executive Summary", "## Consolidated Discussion by Topic", "## Key Decisions", "## Action Items"]

SYSTEM_PROMPT = """You are the minute-taker of an engineering team meeting (photovoltaic plant operations).
You receive a time-ordered transcript where each line is `[Speaker] text`. Speech-to-text noise
(misspellings, split words, mixed Italian/English) is expected: interpret charitably, never invent facts.
Write the minutes in the language most used in the transcript.

Rules:
1. Map every distinct speaker's arguments, concerns and proposals. Keep minority viewpoints.
2. Merge overlapping points from different participants into unified topic threads, attributing
   with [Speaker Name] in square brackets.
3. Output ONLY Markdown with exactly these sections, in this order:
## Executive Summary
## Consolidated Discussion by Topic
## Key Decisions
## Action Items
4. `## Action Items` is a Markdown table with header `| Owner | Task | Deadline |`. Owner is a
   speaker name from the transcript (or `Unassigned`). Deadline is YYYY-MM-DD or empty.
No preamble, no closing remarks, no code fences."""

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


class SynthesisUnavailable(Exception):
    pass


def transcript_lines(segments: list[dict]) -> list[str]:
    return [f"[{s['speaker']}] {s['text']}" for s in segments if s.get("text", "").strip()]


def transcript_markdown(segments: list[dict]) -> str:
    """Fallback when the LLM is unavailable: the raw attributed transcript."""
    body = "\n".join(f"**[{s['speaker']}]** {s['text']}" for s in segments if s.get("text", "").strip())
    return f"## Transcript\n\n{body}\n"


def build_messages(segments: list[dict], meeting_id: str) -> list[dict]:
    speakers = sorted({s["speaker"] for s in segments})
    user = (f"Meeting id: {meeting_id}\nParticipants: {', '.join(speakers)}\n\nTranscript:\n"
            + "\n".join(transcript_lines(segments)))
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def clean_response(text: str) -> str:
    text = _THINK.sub("", text).strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1)
    return text.strip() + "\n"


def synthesize(segments: list[dict], meeting_id: str, url: str, model: str, timeout: float = 300) -> str:
    payload = {"model": model, "messages": build_messages(segments, meeting_id), "stream": False,
               "options": {"temperature": 0.2}}
    req = urllib.request.Request(f"{url.rstrip('/')}/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        content = data["message"]["content"]
    except (urllib.error.URLError, OSError, KeyError, ValueError, TypeError) as e:
        raise SynthesisUnavailable(f"Ollama at {url} failed: {e}") from e
    md = clean_response(content)
    if "## Action Items" not in md:
        md += "\n## Action Items\n\n| Owner | Task | Deadline |\n| --- | --- | --- |\n"
    return md
