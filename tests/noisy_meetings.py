"""Write a set of deliberately messy meetings into a data directory, to exercise the dashboard and viewer.

    python tests/noisy_meetings.py C:/tmp/noisy-data
    $env:DATA_DIR="C:/tmp/noisy-data"; python -m uvicorn server.hub:app --port 8085   # then open /dashboard

Covers: STT noise and fillers, HTML/script-looking text, unbroken long tokens, emoji and accents, very long
speaker names, a 3-hour meeting with 1500 sentences, torn and corrupt JSONL lines, a corrupt finalize record,
LLM fallback with a long error, messy LLM Markdown, an Odoo-published meeting, and an empty file.
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

SPEAKERS = ["Muhammad", "Gabriele", "Ginadomenico", "Remote-Call", "Anna-Maria Esposito-Rossi", "X"]
LINES = [
    "ehm ok allora partiamo dal tracker report di Mazara",
    "the drop matches a grid event not a hardware fault",
    "uh uh si si esatto",
    "<img src=x onerror=alert('xss')> questo non deve eseguire nulla",
    "**bold?** `code?` | pipe | in a sentence",
    "https://example.invalid/a/very/long/path/without/any/spaces/that/should/wrap/instead/of/overflowing/the/layout/abc123",
    "venerdì 🙂 caffè perché così",
    "ok",
    "Gabriele puoi verificare i dati dell'inverter entro venerdì",
    "[inaudible] … … …",
    "I will export the VCOM data and share it by Friday",
    "il tracker T12 ha perso la comunicazione per due ore",
]
MESSY_MINUTES = """## Executive Summary
The team reviewed **tracker T12** comms loss; root cause is a *grid event*, not hardware.

## Consolidated Discussion by Topic
- [Muhammad] proposed automatic checks on SCADA logs
  - nested item that the renderer flattens
- [Gabriele] <script>alert(1)</script> must render as text
1) numbered with parenthesis
2) second

## Key Decisions
Plain paragraph with `inline code` and an unclosed **bold

## Action Items
| Owner | Task | Deadline |
|:------|-----:|:--------:|
| **Gabriele** | Export VCOM data | 2026-11-27 |
| Muhammad | Add SCADA log check with a really long description that needs to wrap inside the table cell without breaking the page layout on a phone | |
| Ginadomenico | Update PR template | 27/11/2026 |
"""


def write(data: Path, mid: str, rows: list, minutes: str | None = None, info: dict | str | None = None,
          raw_tail: str = "") -> None:
    with (data / f"{mid}.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.write(raw_tail)
    if minutes is not None:
        (data / f"{mid}.minutes.md").write_text(minutes, encoding="utf-8")
    if info is not None:
        (data / f"{mid}.finalize.json").write_text(info if isinstance(info, str) else json.dumps(info), encoding="utf-8")


def meeting(start: float, n: int, speakers: list[str], rng: random.Random) -> list[dict]:
    t, out = start, []
    for i in range(n):
        t += rng.uniform(2, 9)
        out.append({"speaker": rng.choice(speakers), "text": rng.choice(LINES), "start": i * 2.0, "end": i * 2.0 + 2,
                    "ts": t, "rms": round(rng.uniform(0.001, 0.3), 4)})
    return out


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(7)
    now = time.time()
    write(out, "weekly-mazara-3h", meeting(now - 4 * 3600, 1500, SPEAKERS, rng))
    write(out, "noisy-fallback", meeting(now - 26 * 3600, 40, SPEAKERS[:4], rng), minutes="## Transcript\n\n**[Muhammad]** ok",
          info={"finalized_at": now - 25 * 3600, "llm": False, "llm_model": "qwen3.5:9b", "odoo": {"status": "saved_locally",
                "path": "/app/data/meetings/noisy-fallback.minutes.md", "reason": "odoo not configured"},
                "llm_error": "Ollama at http://host.docker.internal:11434 failed: HTTP Error 500: llama-server reported "
                             "out-of-memory during startup: cudaMalloc failed: out of memory " + "x" * 300})
    write(out, "messy-llm-minutes", meeting(now - 50 * 3600, 25, SPEAKERS[:3], rng), minutes=MESSY_MINUTES,
          info={"finalized_at": now - 49 * 3600, "llm": True, "llm_model": "qwen3.5:9b", "llm_error": None,
                "odoo": {"status": "saved_locally", "path": "x", "reason": "ProtocolError: <ProtocolError for odoo:8069/xmlrpc/2/common: 502 Bad Gateway>"}})
    write(out, "published-odoo", meeting(now - 80 * 3600, 30, SPEAKERS[:3], rng), minutes=MESSY_MINUTES,
          info={"finalized_at": now - 79 * 3600, "llm": True, "llm_model": "gemma4", "llm_error": None,
                "odoo": {"status": "published", "parent_task_id": 4711, "subtasks": [{"subtask_id": 1}, {"subtask_id": 2}, {"subtask_id": 3}], "path": "x"}})
    write(out, "torn-and-corrupt", meeting(now - 100 * 3600, 12, SPEAKERS[:2], rng), info="{not json",
          raw_tail='{"speaker": "Gabriele", "text": "cut mid wr\nnot json\n[1,2]\n{"speaker": 5}\n')
    write(out, "weekly_operations_and_maintenance_review_for_mazara_plant_2026", meeting(now - 120 * 3600, 5, ["Anna-Maria Esposito-Rossi"], rng))
    (out / "empty-file.jsonl").write_text("", encoding="utf-8")
    (out / "old-format-minutes.jsonl").write_text(json.dumps({"speaker": "Muhammad", "text": "legacy", "start": 0, "end": 1, "ts": now - 200 * 3600, "rms": 0.1}) + "\n", encoding="utf-8")
    (out / "old-format-minutes.minutes.md").write_text("## Executive Summary\nLegacy minutes file without a record.\n", encoding="utf-8")
    print(f"wrote noisy meetings to {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "data/noisy"))
