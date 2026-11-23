"""Audio sources: microphone (sounddevice) and Windows WASAPI loopback (PyAudioWPatch).
Both deliver 16 kHz mono float32 blocks via `read()`, as the hub expects (CONTRACTS.md §1.1)."""
from __future__ import annotations

import logging
import queue
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)
RATE = 16000


class MicCapture:
    def __init__(self, device: int | str | None = None, samplerate: int = RATE,
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

    def start(self) -> bool:
        self._stream = self._factory(self._callback)
        self._stream.start()
        return True

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


class LoopbackCapture:
    """What the PC plays (the remote side of a call), via the default speakers' WASAPI loopback."""

    def __init__(self, blocksize: int = 4096, _stream_factory: Callable | None = None) -> None:
        self.blocksize = blocksize
        self.muted = False
        self.src_rate, self.channels = 48000, 2  # overwritten from the device when opened
        self._q: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self._buf = np.zeros(0, dtype=np.float32)
        self._factory = _stream_factory or self._wasapi_stream
        self._stream = None

    def _wasapi_stream(self, callback):
        import pyaudiowpatch as pyaudio

        p = pyaudio.PyAudio()
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        speakers = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if not speakers.get("isLoopbackDevice"):
            for lb in p.get_loopback_device_info_generator():
                if speakers["name"] in lb["name"]:
                    speakers = lb
                    break
            else:
                raise RuntimeError("no WASAPI loopback device for the default speakers")
        self.src_rate, self.channels = int(speakers["defaultSampleRate"]), int(speakers["maxInputChannels"])
        return p.open(format=pyaudio.paInt16, channels=self.channels, rate=self.src_rate, input=True,
                      input_device_index=speakers["index"], frames_per_buffer=2048, stream_callback=callback)

    def _callback(self, in_data: bytes, frame_count, time_info, status):
        pcm = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
        if self.channels > 1:
            pcm = pcm.reshape(-1, self.channels).mean(axis=1)
        if self.src_rate != RATE:
            # ponytail: per-chunk linear resample; fine for speech, swap for a polyphase filter if quality matters
            n = int(round(len(pcm) * RATE / self.src_rate))
            pcm = np.interp(np.linspace(0, len(pcm), n, endpoint=False), np.arange(len(pcm)), pcm).astype(np.float32)
        self._buf = np.concatenate((self._buf, pcm))
        while len(self._buf) >= self.blocksize:
            block, self._buf = self._buf[:self.blocksize], self._buf[self.blocksize:]
            try:
                self._q.put_nowait(block.tobytes())
            except queue.Full:
                pass
        return (None, 0)  # paContinue

    def start(self) -> bool:
        try:
            self._stream = self._factory(self._callback)
            (getattr(self._stream, "start_stream", None) or self._stream.start)()
            return True
        except Exception as e:  # module missing, no loopback device, device busy
            log.warning("system audio capture unavailable: %s", e)
            self._stream = None
            return False

    def stop(self) -> None:
        if self._stream is not None:
            (getattr(self._stream, "stop_stream", None) or self._stream.stop)()
            self._stream.close()
            self._stream = None

    read = MicCapture.read
