"""Same-room cross-talk suppression: when two workstations hear the same sentence at the same
absolute audio time, keep the louder stream (the real speaker) and drop the mic bleed."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
MIN_WORDS = 4

# Phrases Whisper is known to invent over silence or hum (seen on the office server: 18 h of an idle mic
# produced 2,127 of them). ponytail: fixed list; extend it when new phantom phrases show up in transcripts.
STOCK_HALLUCINATIONS = {
    "thank you", "thank you very much", "thanks for watching", "thank you for watching", "bye", "you",
    "감사합니다", "ご視聴ありがとうございました", "sottotitoli creati dalla comunità amaraorg",
    "sottotitoli a cura di qtss", "subtitles by the amaraorg community",
}


def is_room_noise(text: str, rms: float, floor: float) -> bool:
    """True for captions that are almost certainly not speech: quieter than `floor`, or a very short or
    stock phrase that is also quiet (below 4x the floor). floor <= 0 disables the filter.
    Measured on real data: floor 0.005 drops 58% of phantom captions (0.008: 69%) and none of the real speech."""
    if floor <= 0:
        return False
    if rms < floor:
        return True
    words = normalize_text(text)
    return rms < 4 * floor and (words in STOCK_HALLUCINATIONS or len(words.split()) <= 2)  # shorter utterances ("ok", "sì") are legitimately said by several people


def normalize_text(s: str) -> str:
    return " ".join(_PUNCT.sub(" ", s.lower()).split())


def text_similarity(a: str, b: str) -> float:
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    short, long_ = sorted((na, nb), key=len)
    if len(short.split()) >= MIN_WORDS and short in long_:
        return 1.0  # bleed often catches only part of the sentence
    return SequenceMatcher(None, na, nb).ratio()


class CrossTalkFilter:
    def __init__(self, window_s: float = 2.0, similarity: float = 0.78, keep_s: float = 20.0) -> None:
        self.window_s, self.similarity, self.keep_s = window_s, similarity, keep_s
        self._frames: list[tuple[int, dict]] = []  # (arrival order, frame)
        self._n = 0

    def add(self, frame: dict) -> None:
        self._n += 1
        self._frames.append((self._n, frame))
        self.prune(frame["ts"])

    def prune(self, now: float) -> None:
        # ponytail: linear list, bounded by keep_s worth of captions per room.
        self._frames = [(i, f) for i, f in self._frames if now - f["ts"] <= self.keep_s]

    def _order(self, frame: dict) -> int:
        for i, f in self._frames:
            if f is frame or (f["speaker"] == frame["speaker"] and f["start"] == frame["start"]):
                return i
        return self._n + 1  # unknown frame: treat as newest

    def should_emit(self, frame: dict) -> bool:
        if len(normalize_text(frame["text"]).split()) < MIN_WORDS:
            return True
        mine = self._order(frame)
        for i, other in self._frames:
            if other["speaker"] == frame["speaker"]:
                continue
            if abs(other["ts"] - frame["ts"]) > self.window_s:
                continue
            if len(normalize_text(other["text"]).split()) < MIN_WORDS:
                continue
            if text_similarity(frame["text"], other["text"]) < self.similarity:
                continue
            if other["rms"] > frame["rms"] or (other["rms"] == frame["rms"] and i < mine):
                return False
        return True
