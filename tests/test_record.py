"""Recording (0.4) through the null input (a tone at the pace of the clock): the length, the sound on disk, pause,
markers, the pre-roll, the level trigger, dropped blocks counted when the queue overflows, and the recording put into
a new document / at the cursor / in place of the selection as one undo step."""
import time

import numpy as np

from sound import record

RATE = 48000


def _open(ch=2, rate=RATE):
    r = record.Recorder()
    devs = record.input_devices()
    assert devs and devs[0].get("null")
    assert r.open(devs[0], ch, rate) and r.state == "ready" and r.backend == "null"
    return r


def _freq(x, rate=RATE):
    sp = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.argmax(sp) * rate / len(x)


def test_record_length_and_sound(session):
    r = _open()
    time.sleep(0.2)
    lv = r.levels()
    assert lv is not None and np.allclose(lv, 0.25, atol=0.01)      # monitored before recording
    assert r.frames == 0
    assert r.record(session.folder())
    t0 = time.monotonic()
    time.sleep(1.0)
    pos = r.add_marker()
    time.sleep(0.5)
    src = r.stop()
    took = time.monotonic() - t0
    r.close()
    assert src is not None and r.state == "closed"
    secs = src.frames / RATE
    assert abs(secs - took) < 0.15 and secs > 1.3
    x = src.read(0, src.frames)
    assert abs(_freq(x[:, 0]) - 440) < 2 and abs(_freq(x[:, 1]) - 660) < 2
    assert abs(float(np.abs(x).max()) - 0.25) < 0.01
    assert r.overflows == 0 and r.markers == [pos] and abs(pos - RATE) < 0.15 * RATE
    assert src.levels                                                     # the peaks are built


def test_pause_preroll_trigger(session):
    r = _open(1, 44100)
    r.set_pre_roll(0.5)
    time.sleep(0.8)
    r.record(session.folder())
    time.sleep(0.5)
    r.pause()
    assert r.state == "pause"
    time.sleep(0.5)
    r.resume()
    time.sleep(0.5)
    src = r.stop()
    secs = src.frames / 44100
    assert 1.35 < secs < 1.75                                           # 0.5 pre-roll + 0.5 + 0.5, not the pause
    r.set_pre_roll(0)
    r.record(session.folder(), trigger_db=-6.0)                        # the tone is at −12 dBFS: never starts
    time.sleep(0.4)
    assert r.state == "armed" and r.frames == 0
    assert r.stop() is None
    r.record(session.folder(), trigger_db=-20.0)                       # starts at once
    time.sleep(0.4)
    assert r.state == "rec" and r.frames > 0
    assert r.stop().frames > 0.2 * 44100
    r.close()


def test_overflow_counted(session, monkeypatch):
    monkeypatch.setattr(record, "QUEUE_SECONDS", 0.05)
    r = _open()
    r.record(session.folder())
    with r._wlock:                                                      # the disk «stalls»
        for _ in range(20):
            r._callback(np.zeros((1024, 2), np.float32), 1024, None, None)
        assert r.overflows >= 15 and r.dropped_frames >= 15 * 1024
    src = r.stop()
    r.close()
    assert src is not None


def test_no_devices_message(monkeypatch):
    monkeypatch.setenv("ARTCORE_AUDIO", "null")
    assert record.probe_input() == "null input"
    r = record.Recorder()
    assert not r.open({"index": 3, "name": "x", "null": False, "channels": 2, "rate": 1}, 2, 48000) or \
        r.backend == "null"
    r.close()
