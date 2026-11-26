"""Turns a meeting's completed captions into consolidated Markdown minutes via local Ollama."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime

SECTIONS = ["## Executive Summary", "## Consolidated Discussion by Topic", "## Key Decisions", "## Action Items"]
LANG_NAMES = {"it": "Italian", "en": "English", "de": "German", "fr": "French", "es": "Spanish"}

MINUTES_SCHEMA = {
    "type": "object",
    "properties": {
        "executive_summary": {"type": "string"},
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "summary": {"type": "string"},
                    "speakers": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["topic", "summary"],
            },
        },
        "key_decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "task": {"type": "string"},
                    "deadline": {"type": "string"},
                },
                "required": ["owner", "task", "deadline"],
            },
        },
    },
    "required": ["executive_summary", "topics", "key_decisions", "action_items"],
}
CHUNK_CHARS = 12000

SYSTEM_PROMPT = """You are the executive minute-taker of an engineering team meeting.
You receive a time-ordered transcript where each line is `[Speaker] text`. Speech-to-text noise
(misspellings, repeated filler syllables, mixed Italian/English) is expected: interpret charitably,
ignore filler/noise, and NEVER invent facts.

Rules:
1. NEVER copy-paste or quote the raw transcript verbatim. Synthesize and summarize into concise,
   well-structured prose.
2. Group discussion by clear topics, attributing speakers accurately.
3. Write ALL text values in the specified Target Language.
4. Resolve relative deadlines ("next week", "prossima settimana", "next month") using Today's Date
   into YYYY-MM-DD (or "" if none).
5. Return ONLY valid JSON matching the schema with keys:
   executive_summary, topics ([{topic, summary, speakers}]), key_decisions ([str]),
   action_items ([{owner, task, deadline}]).
   (Required Markdown headings when rendered: ## Executive Summary, ## Consolidated Discussion by Topic,
   ## Key Decisions, ## Action Items.)"""

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


def render_minutes_json(data: dict) -> str:
    """Render deterministic Markdown from structured Ollama JSON output."""
    summary = (data.get("executive_summary") or "").strip() or "No summary."
    topics = []
    for t in data.get("topics") or []:
        title = str(t.get("topic") or "Discussion").strip()
        body = str(t.get("summary") or "").strip()
        sp = ", ".join(str(s).strip() for s in (t.get("speakers") or []) if str(s).strip())
        if body:
            topics.append(f"- **{title}**: {body}" + (f" [{sp}]" if sp else ""))
    decisions = [f"- {str(d).strip()}" for d in (data.get("key_decisions") or []) if str(d).strip()]
    rows = []
    for item in data.get("action_items") or []:
        owner = str(item.get("owner") or "Unassigned").replace("|", "/").strip() or "Unassigned"
        task = str(item.get("task") or "").replace("|", "/").strip()
        dl = str(item.get("deadline") or "").replace("|", "/").strip()
        if task:
            rows.append(f"| {owner} | {task} | {dl} |")
    parts = [
        "## Executive Summary", summary, "",
        "## Consolidated Discussion by Topic", "\n".join(topics) if topics else "- No topics recorded.", "",
        "## Key Decisions", "\n".join(decisions) if decisions else "- None.", "",
        "## Action Items", "", "| Owner | Task | Deadline |", "| --- | --- | --- |",
        *rows,
    ]
    return "\n".join(parts).strip() + "\n"


def _call_ollama(messages: list[dict], url: str, model: str, timeout: float) -> str:
    payload = {"model": model, "messages": messages, "stream": False, "format": MINUTES_SCHEMA,
               "options": {"temperature": 0.2, "num_ctx": 16384}}
    req = urllib.request.Request(f"{url.rstrip('/')}/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)["message"]["content"]
    except (urllib.error.URLError, OSError, KeyError, ValueError, TypeError) as e:
        raise SynthesisUnavailable(f"Ollama at {url} failed: {e}") from e


def _chunk_segments(segments: list[dict], max_chars: int = CHUNK_CHARS, overlap: int = 5) -> list[list[dict]]:
    non_empty = [s for s in segments if s.get("text", "").strip()]
    if sum(len(s["text"]) + len(s["speaker"]) + 4 for s in non_empty) <= max_chars:
        return [non_empty]
    chunks: list[list[dict]] = []
    i = 0
    while i < len(non_empty):
        cur, chars = [], 0
        j = i
        while j < len(non_empty) and (not cur or chars + len(non_empty[j]["text"]) <= max_chars):
            cur.append(non_empty[j])
            chars += len(non_empty[j]["text"]) + len(non_empty[j]["speaker"]) + 4
            j += 1
        chunks.append(cur)
        i = j if j >= len(non_empty) else max(i + 1, j - overlap)
    return chunks


def _parse_json_or_none(raw: str) -> dict | None:
    cleaned = _FENCE.sub(r"\1", _THINK.sub("", raw).strip()).strip()
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) and "executive_summary" in obj else None
    except ValueError:
        return None


def synthesize(segments: list[dict], meeting_id: str, url: str, model: str, timeout: float = 300,
               language: str = "it") -> str:
    chunks = _chunk_segments(segments)
    if len(chunks) == 1:
        content = _call_ollama(build_messages(segments, meeting_id, language=language), url, model, timeout)
        parsed = _parse_json_or_none(content)
        if parsed is not None:
            return render_minutes_json(parsed)
        md = clean_response(content)
        if "## Action Items" not in md:
            md += "\n## Action Items\n\n| Owner | Task | Deadline |\n| --- | --- | --- |\n"
        return md

    merged: dict = {"executive_summary": [], "topics": [], "key_decisions": [], "action_items": []}
    for idx, ch in enumerate(chunks, 1):
        content = _call_ollama(build_messages(ch, f"{meeting_id} (part {idx}/{len(chunks)})", language=language),
                               url, model, timeout)
        parsed = _parse_json_or_none(content)
        if parsed is None:
            continue
        if parsed.get("executive_summary"):
            merged["executive_summary"].append(str(parsed["executive_summary"]).strip())
        merged["topics"].extend(parsed.get("topics") or [])
        merged["key_decisions"].extend(parsed.get("key_decisions") or [])
        merged["action_items"].extend(parsed.get("action_items") or [])
    merged["executive_summary"] = " ".join(merged["executive_summary"]) or "No summary."
    return render_minutes_json(merged)
