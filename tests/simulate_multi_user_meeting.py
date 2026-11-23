"""End-to-end simulation: N fake workstations stream audio to the hub, one listener checks captions.

    python tests/simulate_multi_user_meeting.py --hub ws://127.0.0.1:8085 --speakers Muhammad,Gabriele
    python tests/simulate_multi_user_meeting.py --hub ws://192.168.10.17:8085 --wav meeting.wav

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


def tone(seconds: float, hz: float) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (0.2 * np.sin(2 * np.pi * hz * t)).astype(np.float32)


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


async def listener(hub: str, meeting: str, captions: list, stop: asyncio.Event) -> None:
    async with connect(f"{hub}/listen/{meeting}", max_size=None) as ws:
        while not stop.is_set():
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.5))
            except asyncio.TimeoutError:
                continue
            if msg["type"] == "caption":
                captions.append(msg)
                if msg["completed"]:
                    print(f"  [{msg['speaker']}] {msg['text']}")
            elif msg["type"] == "roster":
                print(f"  roster: {msg['speakers']}")


async def main(a: argparse.Namespace) -> int:
    names = [n.strip() for n in a.speakers.split(",") if n.strip()]
    pcm = load_wav(a.wav) if a.wav else None
    print(f"meeting={a.meeting} speakers={names} hub={a.hub}")
    captions: list = []
    stop = asyncio.Event()
    lst = asyncio.create_task(listener(a.hub, a.meeting, captions, stop))
    await asyncio.sleep(0.3)
    own: dict[str, list] = {n: [] for n in names}
    await asyncio.gather(*(
        speaker(a.hub, a.meeting, n, pcm if pcm is not None else tone(a.seconds, 220 + 110 * i), own[n])
        for i, n in enumerate(names)))
    await asyncio.sleep(1.0)
    stop.set()
    await lst

    http = a.hub.replace("ws://", "http://").replace("wss://", "https://")
    with urllib.request.urlopen(f"{http}/api/meetings/{a.meeting}/segments", timeout=5) as r:
        persisted = json.load(r)
    done = [c for c in captions if c["completed"]]
    ok = True
    for n in names:
        got = [c for c in done if c["speaker"] == n]
        print(f"{n}: {len(got)} completed captions at listener, {len(own[n])} echoed to speaker")
        ok &= bool(got)
    print(f"persisted={len(persisted)} listener_completed={len(done)}")
    ok &= len(persisted) == len(done)
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--hub", default="ws://127.0.0.1:8085")
    p.add_argument("--meeting", default=f"sim-{int(time.time())}")
    p.add_argument("--speakers", default="Muhammad,Gabriele,Ginadomenico")
    p.add_argument("--wav", default=None)
    p.add_argument("--seconds", type=float, default=8.0)
    sys.exit(asyncio.run(main(p.parse_args())))
