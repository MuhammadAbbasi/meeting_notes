import itertools

import numpy as np
import pytest

from client.audio_capture import LoopbackCapture, MicCapture
from client.hub_link import backoff_delays, speaker_url


def test_loopback_missing_device_is_noop():
    def factory(cb):
        raise RuntimeError("no WASAPI loopback device")

    cap = LoopbackCapture(_stream_factory=factory)
    assert cap.start() is False
    cap.stop()  # must not raise
    assert cap.read(timeout=0.01) is None


def test_loopback_resamples_stereo_48k_to_mono_16k():
    class FakePA:
        def __init__(self, cb):
            self.cb, self.running = cb, False

        def start_stream(self):
            self.running = True

        def stop_stream(self):
            self.running = False

        def close(self):
            pass

    streams = []
    cap = LoopbackCapture(blocksize=4096, _stream_factory=lambda cb: streams.append(FakePA(cb)) or streams[-1])
    cap.src_rate, cap.channels = 48000, 2
    assert cap.start() is True and streams[0].running
    frame = np.full((4800, 2), 16384, dtype=np.int16)  # 0.1 s stereo at 48 kHz, value 0.5
    frame[:, 1] = -16384  # right channel inverted: mono mix must be ~0, so use left-only check below
    frame[:, 1] = 16384
    for _ in range(12):  # 1.2 s -> 19200 samples at 16 kHz -> 4 full blocks
        assert streams[0].cb(frame.tobytes(), 4800, None, None) == (None, 0)
    blocks = [cap.read(timeout=0.01) for _ in range(5)]
    assert blocks[4] is None and all(len(b) == 4096 * 4 for b in blocks[:4])
    assert np.frombuffer(blocks[0], dtype=np.float32).mean() == pytest.approx(0.5, abs=1e-3)
    cap.muted = True
    streams[0].cb(frame.tobytes(), 4800, None, None); streams[0].cb(frame.tobytes(), 4800, None, None)
    streams[0].cb(frame.tobytes(), 4800, None, None)
    assert cap.read() == bytes(4096 * 4)
    cap.stop()
    assert not streams[0].running


def test_backoff_sequence():
    assert list(itertools.islice(backoff_delays(), 7)) == [1, 2, 4, 8, 16, 30, 30]


def test_speaker_url_encodes_name():
    assert speaker_url("192.168.10.17", 8085, "daily", "Gian Domenico", "auto") == \
        "ws://192.168.10.17:8085/ws/daily/Gian%20Domenico?lang=auto"


class FakeStream:
    def __init__(self, callback):
        self.callback, self.started = callback, False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        pass


def test_muted_capture_returns_silence():
    streams = []

    def factory(cb):
        s = FakeStream(cb)
        streams.append(s)
        return s

    cap = MicCapture(blocksize=4096, _stream_factory=factory)
    cap.start()
    assert streams[0].started
    block = np.full((4096, 1), 0.5, dtype=np.float32)
    streams[0].callback(block, 4096, None, None)
    assert cap.read() == block[:, 0].tobytes()
    cap.muted = True
    streams[0].callback(block, 4096, None, None)
    assert cap.read() == bytes(4096 * 4)
    assert cap.read(timeout=0.01) is None
    cap.stop()
    assert not streams[0].started
