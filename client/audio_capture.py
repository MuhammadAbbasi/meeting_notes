"""Microphone capture: 16 kHz mono float32 blocks, as the hub expects (CONTRACTS.md §1.1)."""
from __future__ import annotations

import queue
from typing import Callable


class MicCapture:
    def __init__(self, device: int | str | None = None, samplerate: int = 16000,
                 blocksize: int = 4096, _stream_factory: Callable | None = None) -> None:
        self.device, self.samplerate, self.blocksize = device, samplerate, blocksize
        self.muted = False
        self._q: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self._factory = _stream_factory or self._sounddevice_stream
        self._stream = None

    def _sounddevice_stream(self, callback):
        import sounddevice as sd  # imported lazily so tests run without PortAudio

        return sd.InputStream(device=self.device, samplerate=self.samplerate, channels=1,
                              dtype="float32", blocksize=self.blocksize, callback=callback)

    def _callback(self, indata, frames, time_info, status) -> None:
        data = indata[:, 0] if getattr(indata, "ndim", 1) > 1 else indata
        try:
            self._q.put_nowait(data.tobytes())
        except queue.Full:
            pass  # hub is behind; dropping a 256 ms block beats growing latency

    def start(self) -> None:
        self._stream = self._factory(self._callback)
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def read(self, timeout: float = 1.0) -> bytes | None:
        try:
            data = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        return bytes(len(data)) if self.muted else data
