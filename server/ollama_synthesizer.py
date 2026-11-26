"""Turns a meeting's completed captions into consolidated Markdown minutes via local Ollama."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime

SECTIONS = ["## Executive Summary", "## Consolidated Discussion by Topic", "## Key Decisions", "## Action Items"]
LANG_NAMES = {"it": "Italian", "en": "English", "de": "German", "fr": "French", "es": "Spanish"}

SYSTEM_PROMPT = """You are the executive minute-taker of an engineering team meeting.
You receive a time-ordered transcript where each line is `[Speaker] text`. Speech-to-text noise
(misspellings, repeated filler syllables, mixed Italian/English) is expected: interpret charitably,
ignore filler/noise, and NEVER invent facts.

Rules:
1. NEVER copy-paste or quote the raw transcript verbatim. Synthesize and summarize into concise,
   well-structured prose and bullet points.
2. Group discussion by clear topic headings (`- **Topic**: concise summary [Speaker]`). Map every
   speaker's arguments, concerns, and proposals, keeping minority viewpoints.
3. Write the entire document (summary, topics, decisions, action items) in the specified meeting
   language (keeping the four Markdown section headings exact).
4. Resolve relative deadlines ("next week", "prossima settimana", "next month") using Today's Date.
5. Output ONLY Markdown with these four sections, in this exact order:
## Executive Summary
## Consolidated Discussion by Topic
## Key Decisions
## Action Items
6. `## Action Items` is a Markdown table with header `| Owner | Task | Deadline |`. Owner is a
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


def build_messages(segments: list[dict], meeting_id: str, language: str = "it") -> list[dict]:
    speakers = sorted({s["speaker"] for s in segments})
    today = datetime.now().strftime("%Y-%m-%d")
    lang_label = LANG_NAMES.get(language, language)
    lang_hint = (f"Target Language: {lang_label} ({language}). Write ALL content in {lang_label}.\n"
                 if language and language != "auto" else "")
    user = (f"Today's Date: {today}\nMeeting id: {meeting_id}\n{lang_hint}"
            f"Participants: {', '.join(speakers)}\n\nTranscript:\n" + "\n".join(transcript_lines(segments)))
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def clean_response(text: str) -> str:
    text = _THINK.sub("", text).strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1)
    return text.strip() + "\n"


def synthesize(segments: list[dict], meeting_id: str, url: str, model: str, timeout: float = 300,
               language: str = "it") -> str:
    payload = {"model": model, "messages": build_messages(segments, meeting_id, language=language), "stream": False,
               "options": {"temperature": 0.2, "num_ctx": 16384}}  # explicit ctx: Ollama otherwise reserves the model max (KV cache of many GB)
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
