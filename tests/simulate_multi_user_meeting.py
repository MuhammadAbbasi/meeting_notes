"""End-to-end simulation: N fake workstations stream audio to the hub, one listener checks captions.

    python tests/simulate_multi_user_meeting.py --hub ws://127.0.0.1:8085 --speakers Muhammad,Gabriele
    python tests/simulate_multi_user_meeting.py --hub ws://192.168.10.17:8085 --wav meeting.wav --noise 0.02
    python tests/simulate_multi_user_meeting.py --speakers Muhammad,Ginadomenico --quiet Ginadomenico         --expect-suppressed Ginadomenico --finalize      # with MOCK_SCRIPT=tests/mock_script_bleed.json

Exit 0 when every speaker produced completed captions that reached the listener and the
persisted segment count matches; exit 1 otherwise.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
import wave
from urllib.parse import quote

import numpy as np
from websockets.asyncio.client import connect

RATE, BLOCK = 16000, 4096


def load_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}[width]
    pcm = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    pcm = (pcm - 128) / 128 if width == 1 else pcm / np.iinfo(dtype).max
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    if rate != RATE:
        n = int(len(pcm) * RATE / rate)
        pcm = np.interp(np.linspace(0, len(pcm), n, endpoint=False), np.arange(len(pcm)), pcm)
    return pcm.astype(np.float32)


def tone(seconds: float, hz: float, amplitude: float = 0.2) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (amplitude * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def add_noise(pcm: np.ndarray, sigma: float, seed: int) -> np.ndarray:
    if sigma <= 0:
        return pcm
    rng = np.random.default_rng(seed)
    return np.clip(pcm + rng.normal(0, sigma, len(pcm)), -1, 1).astype(np.float32)


async def speaker(hub: str, meeting: str, name: str, pcm: np.ndarray, log: list) -> None:
    async with connect(f"{hub}/ws/{meeting}/{quote(name)}?lang=auto", max_size=None) as ws:
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", ready
        t0 = time.monotonic()
        for i in range(0, len(pcm), BLOCK):
            await ws.send(pcm[i:i + BLOCK].tobytes())
            await asyncio.sleep(max(0.0, t0 + (i + BLOCK) / RATE - time.monotonic()))
            while True:  # drain our own copy of the broadcasts without blocking the pace
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.001))
                except asyncio.TimeoutError:
                    break
                if msg["type"] == "caption" and msg["completed"] and msg["speaker"] == name:
                    log.append(msg)
        await ws.send(json.dumps({"type": "end"}))
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                if msg["type"] == "caption" and msg["completed"] and msg["speaker"] == name:
                    log.append(msg)
        except Exception:
            pass


async def listener(hub: str, meeting: str, captions: list, stop: asyncio.Event, quiet: bool = False,
                   latencies: list | None = None) -> None:
    async with connect(f"{hub}/listen/{meeting}", max_size=None) as ws:
        while not stop.is_set():
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.5))
            except asyncio.TimeoutError:
                continue
            if quiet:  # extra screens only measure how late completed captions arrive
                if msg["type"] == "caption" and msg["completed"] and latencies is not None:
                    latencies.append(time.time() - (msg["ts"] + msg["end"] - msg["start"]))  # ts marks the sentence start
                continue
            if msg["type"] == "caption":
                captions.append(msg)
                if msg["completed"]:
                    print(f"  [{msg['speaker']}] {msg['text']}")
            elif msg["type"] == "retract":
                captions.append(msg)
                print(f"  ~ retracted bleed from {msg['speaker']}")
            elif msg["type"] == "roster":
                print(f"  roster: {msg['speakers']}")


async def main(a: argparse.Namespace) -> int:
    names = [n.strip() for n in a.speakers.split(",") if n.strip()]
    pcm = load_wav(a.wav) if a.wav else None
    print(f"meeting={a.meeting} speakers={names} hub={a.hub}")
    captions: list = []
    stop = asyncio.Event()
    lst = asyncio.create_task(listener(a.hub, a.meeting, captions, stop))
    latencies: list = []
    extra = [asyncio.create_task(listener(a.hub, a.meeting, [], stop, quiet=True, latencies=latencies)) for _ in range(a.listeners)]
    await asyncio.sleep(0.3)
    own: dict[str, list] = {n: [] for n in names}
    quiet = {q.strip() for q in a.quiet.split(",") if q.strip()}

    def audio_for(i: int, n: str) -> np.ndarray:
        base = pcm if pcm is not None else tone(a.seconds, 220 + 110 * i)
        if n in quiet:
            base = base * 0.1  # a mic hearing someone else across the room
        return add_noise(base, a.noise, seed=i)

    await asyncio.gather(*(speaker(a.hub, a.meeting, n, audio_for(i, n), own[n]) for i, n in enumerate(names)))
    await asyncio.sleep(a.settle)  # > hub DEDUP_HOLD_S so held captions are decided
    stop.set()
    await lst
    await asyncio.gather(*extra)

    http = a.hub.replace("ws://", "http://").replace("wss://", "https://")
    with urllib.request.urlopen(f"{http}/api/meetings/{a.meeting}/segments", timeout=5) as r:
        persisted = json.load(r)
    done = [c for c in captions if c["type"] == "caption" and c["completed"]]
    suppressed = {q.strip() for q in a.expect_suppressed.split(",") if q.strip()}
    ok = True
    for n in names:
        got = [c for c in done if c["speaker"] == n]
        print(f"{n}: {len(got)} completed captions at listener")
        ok &= bool(got) or n in suppressed
    print(f"persisted={len(persisted)} listener_completed={len(done)}")
    if a.listeners:
        expect = len(done) * a.listeners
        lat = sorted(latencies)
        p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] if lat else float("nan")  # noqa: E731
        print(f"extra screens={a.listeners}: received {len(lat)}/{expect} completed captions, "
              f"delay after sentence end p50={p(.5):.2f}s p95={p(.95):.2f}s max={p(1):.2f}s")
        ok_screens = len(lat) == expect
    ok &= len(persisted) == len(done)
    if a.listeners:
        ok &= ok_screens
    retracted = [c for c in captions if c["type"] == "retract"]
    if retracted:
        print(f"retracted (bleed suppressed): {sorted({r['speaker'] for r in retracted})} x{len(retracted)}")
    for n in sorted(suppressed):
        leaked = [p for p in persisted if p["speaker"] == n]
        print(f"{n}: expected suppressed, persisted={len(leaked)}")
        ok &= not leaked
    if a.finalize:
        req = urllib.request.Request(f"{http}/api/meetings/{a.meeting}/finalize", method="POST")
        with urllib.request.urlopen(req, timeout=900) as r:
            fin = json.load(r)
        print(f"finalize: llm={fin['llm']} odoo={fin['odoo']['status']} saved_to={fin['saved_to']}")
        print("--- minutes (head) ---")
        print("\n".join(fin["markdown"].splitlines()[:12]))
        ok &= bool(fin["markdown"].strip())
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--hub", default="ws://127.0.0.1:8085")
    p.add_argument("--meeting", default=f"sim-{int(time.time())}")
    p.add_argument("--speakers", default="Muhammad,Gabriele,Ginadomenico")
    p.add_argument("--wav", default=None)
    p.add_argument("--seconds", type=float, default=8.0)
    p.add_argument("--quiet", default="", help="comma-separated speakers whose audio is 10x quieter (bleed)")
    p.add_argument("--noise", type=float, default=0.0, help="gaussian noise sigma added to all audio, e.g. 0.02")
    p.add_argument("--expect-suppressed", default="", help="speakers that must have 0 persisted captions")
    p.add_argument("--settle", type=float, default=3.5, help="seconds to wait after the last audio (> DEDUP_HOLD_S)")
    p.add_argument("--finalize", action="store_true", help="call /finalize at the end and print the minutes head")
    p.add_argument("--listeners", type=int, default=0, help="extra caption screens that must all receive every caption")
    sys.exit(asyncio.run(main(p.parse_args())))
