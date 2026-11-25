"""Score a recorded meeting's captions against the script that was spoken.

    python tests/score_meeting.py --hub http://HUB:8085 --meeting panel-cleaning-10 --reference OUT/reference.json

Per person: share of their words that appear in their own captions (recall), and words in their captions
that only someone else said (misattributed, e.g. same-room bleed that was not suppressed).
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.request

word = lambda t: re.findall(r"\w+", t.lower())  # noqa: E731


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--hub", default="http://127.0.0.1:8085")
    p.add_argument("--meeting", required=True)
    p.add_argument("--reference", required=True)
    a = p.parse_args()
    ref = json.load(open(a.reference, encoding="utf-8"))
    with urllib.request.urlopen(f"{a.hub}/api/meetings/{a.meeting}/segments", timeout=10) as r:
        segs = json.load(r)
    said = {}
    for r in ref:
        said.setdefault(r["speaker"], []).extend(word(r["text"]))
    total_said = total_got = 0
    print(f"{'person':13} {'lang':4} {'said':>5} {'recovered':>10} {'recall':>7} {'captions':>9} {'misattributed':>14}")
    for who, words in said.items():
        mine = set(words)
        others = set(w for o, ws in said.items() if o != who for w in ws) - mine
        cap = [w for s in segs if s["speaker"] == who for w in word(s["text"])]
        got = sum(1 for w in set(words) if w in set(cap))
        wrong = sum(1 for w in cap if w in others)
        lang = next(r["lang"] for r in ref if r["speaker"] == who)
        total_said += len(set(words)); total_got += got
        print(f"{who:13} {lang:4} {len(set(words)):5} {got:10} {100 * got // max(1, len(set(words))):6}% "
              f"{sum(1 for s in segs if s['speaker'] == who):9} {wrong:14}")
    print(f"overall recall {100 * total_got // max(1, total_said)}% of distinct words, {len(segs)} captions stored")


if __name__ == "__main__":
    main()
