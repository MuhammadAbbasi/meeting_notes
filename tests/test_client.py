import itertools

import numpy as np

from client.audio_capture import MicCapture
from client.hub_link import backoff_delays, speaker_url


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
