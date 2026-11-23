"""The listening volume (2.0): the null player's output is quieter by the set dB, a change on the fly ramps without
jumps, the meters and the live-spectrum tap still show the file, render() and saving are not touched, the volume
is kept in the config; the transport's slider, the wheel, the mute button, the keys and the menu; the input monitor
of the Record window plays the input at the same volume."""
import os
import time

import numpy as np
from conftest import pump
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent

from sound import config, media, player, record
from sound.player import VOL_MIN_DB, GainRamp, InputMonitor, Player, Volume


def _rms(x):
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


def _play_capture(d, vol, seconds=0.5, start=0):
    blocks = []
    p = Player(volume=vol)
    p.sink = blocks.append
    assert p.play(d, start, d.frames) and p.backend == "null"
    time.sleep(seconds)
    lv = p.levels().copy()
    tap = p.tap(4096)
    p.stop()
    return np.concatenate(blocks[2:-1]), lv, tap          # steady blocks (not the first / last)


def test_volume_object():
    v = Volume(-100)
    assert v.db == VOL_MIN_DB and v.gain() == 0.0
    v.set(6)
    assert v.db == 0.0 and v.gain() == 1.0
    v.set(-12)
    assert abs(20 * np.log10(v.gain()) + 12) < 1e-9
    v.set(muted=True)
    assert v.gain() == 0.0
    v.step(2)                                           # louder while muted: unmutes
    assert not v.muted and v.db == -10


def test_gain_ramp_smooth_and_exact():
    rate = 48000
    vol = Volume(0)
    r = GainRamp(vol)
    x = np.ones((1024, 2), np.float32)
    r.apply(x, 1024, rate)
    assert np.all(x == 1)                               # 0 dB: untouched
    vol.set(-20)
    out = []
    for _ in range(8):
        x = np.ones((256, 2), np.float32)
        r.apply(x, 256, rate)
        out.append(x[:, 0].copy())
    y = np.concatenate(out)
    step = np.max(np.abs(np.diff(y)))
    assert step <= 1.0 / (player.VOL_RAMP * rate) + 1e-6   # no jump: at most one ramp step per sample
    assert abs(y[-1] - 0.1) < 1e-6 and abs(y[0] - 1.0) < 0.01
    assert np.all(np.diff(y) <= 1e-7)                   # monotonic, no overshoot
    vol.set(muted=True)
    x = np.ones((4096, 2), np.float32)
    r.apply(x, 4096, rate)
    assert x[0, 0] > 0.09 and x[-1, 0] == 0.0 and np.max(np.abs(np.diff(x[:, 0]))) < 0.002


def test_player_output_at_volume(media_dir, session):
    d = media.open_file(os.path.join(media_dir, "stereo16.wav"), session.folder())
    full, lv0, tap0 = _play_capture(d, Volume(0))
    quiet, lv1, tap1 = _play_capture(d, Volume(-12))
    db = 20 * np.log10(_rms(quiet) / _rms(full))
    assert abs(db + 12) < 0.1, db
    # the meters and the tap show the file's own level, not the listening volume
    assert np.allclose(lv0, lv1, atol=0.01) and abs(lv1[0] - 0.5) < 0.02
    assert tap1 is not None and abs(np.max(np.abs(tap1[:, 0])) - 0.5) < 0.02
    muted, lv2, _t = _play_capture(d, Volume(0, muted=True))
    assert np.max(np.abs(muted)) == 0 and abs(lv2[0] - 0.5) < 0.02


def test_volume_changes_on_the_fly_without_clicks(media_dir, session):
    d = media.open_file(os.path.join(media_dir, "mono24.wav"), session.folder())     # 1 kHz sine
    vol = Volume(0)
    blocks = []
    p = Player(volume=vol)
    p.sink = blocks.append
    assert p.play(d, 0, d.frames)
    time.sleep(0.25)
    n_before = len(blocks)
    vol.set(-30)
    time.sleep(0.3)
    p.stop()
    y = np.concatenate(blocks)[:, 0]
    env = np.abs(y)
    # the envelope of a steady sine falls along a ramp, never by more than one ramp step a sample
    peaks = np.array([env[i:i + 48].max() for i in range(0, len(env) - 48, 48)])
    amp = peaks[2]                                          # ffmpeg's sine: 1/8
    assert amp > 0.1 and peaks[2: n_before * 1024 // 48 - 2].min() > 0.97 * amp
    assert abs(20 * np.log10(peaks[-20:].max() / amp) + 30) < 0.5
    falls = -np.diff(peaks)
    # a full-scale change takes VOL_RAMP, so 48 samples move the gain by at most 48 / (VOL_RAMP·rate)
    assert falls.max() < amp * 48 / (player.VOL_RAMP * 48000) + 0.01 * amp
    assert (falls > 0.01 * amp).sum() >= 2                  # spread over several windows, not one step


def test_render_and_save_not_touched(media_dir, session, tmp_path):
    d = media.open_file(os.path.join(media_dir, "stereo16.wav"), session.folder())
    before = d.read(0, d.frames).copy()
    p = Player(volume=Volume(-24, muted=True))
    assert p.play(d, 0, 4410)
    time.sleep(0.15)
    p.stop()
    assert np.array_equal(d.read(0, d.frames), before)
    out = str(tmp_path / "out.wav")
    media.save(d, out, "wav", "32f")
    e = media.open_file(out, session.folder())
    assert np.max(np.abs(e.read(0, e.frames) - before)) < 1e-6


def test_input_monitor_at_volume():
    vol = Volume(-6)
    r = record.Recorder()
    devs = record.input_devices()
    assert r.open(devs[0], 2, 48000)
    blocks = []
    m = InputMonitor(volume=vol)
    m.sink = blocks.append
    assert m.open(48000, 2) and m.backend == "null"
    r.monitor = m
    time.sleep(0.4)
    r.monitor = None
    m.close()
    r.close()
    y = np.concatenate(blocks[4:-1])
    assert len(y) > 4096
    peak = np.max(np.abs(y[:, 0]))
    assert abs(20 * np.log10(peak / 0.25) + 6) < 0.3          # the null input's −12 dBFS tone, 6 dB quieter
    assert m._qframes == 0


def _win(qapp, session, **over):
    from sound.window import MainWindow
    cfg = config.load()
    cfg.update(check_updates=False, geometry="")
    cfg.update(over)
    w = MainWindow(cfg, session)
    w.resize(1400, 900)
    w.show()
    pump(qapp, 0.1)
    return w


def test_transport_control_keys_menu_config(qapp, session, data_dir):
    w = _win(qapp, session)
    assert w.volume is w.player.volume and w.volume.db == 0.0
    w.vol.slider.setValue(-180)
    assert abs(w.volume.db + 18) < 1e-9
    assert "−18.0 dB" in w.vol.slider.toolTip() and "13 %" in w.vol.slider.toolTip()
    # the wheel: 1 dB a notch
    ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, 240), Qt.NoButton, Qt.NoModifier,
                     Qt.NoScrollPhase, False)
    w.vol.slider.wheelEvent(ev)
    assert abs(w.volume.db + 16) < 1e-9
    # the keys and the menu
    assert w.a_vol_up.shortcut().toString() == "Ctrl+Alt+Up"
    assert w.a_vol_down.shortcut().toString() == "Ctrl+Alt+Down"
    assert w.a_mute.shortcut().toString() == "Ctrl+Alt+M"
    w.a_vol_down.trigger()
    assert abs(w.volume.db + 18) < 1e-9 and w.vol.slider.value() == -180
    w.a_vol_up.trigger()
    w.a_vol_up.trigger()
    assert abs(w.volume.db + 14) < 1e-9
    w.vol.button.click()
    assert w.volume.muted and w.a_mute.isChecked() and w.volume.gain() == 0
    w.a_mute.trigger()
    assert not w.volume.muted and not w.a_mute.isChecked()
    w.a_mute.trigger()
    assert w.volume.muted
    pump(qapp, 1.0)                                          # the delayed save
    saved = config.load()
    assert saved["play_volume_db"] == -14.0 and saved["play_muted"] is True
    w.close()
    w2 = _win(qapp, session)
    assert w2.volume.db == -14.0 and w2.volume.muted and w2.vol.slider.value() == -140 and w2.a_mute.isChecked()
    w2.close()


def test_record_window_monitor(qapp, session, data_dir):
    w = _win(qapp, session, record={"monitor": False})
    w.record_toggle()
    pump(qapp, 0.2)
    dlg = w.rec_dlg
    assert dlg is not None and not dlg.mon_on.isChecked() and dlg.rec.monitor is None and not dlg.monitor.on
    dlg.mon_on.setChecked(True)
    pump(qapp, 0.2)
    assert dlg.rec.monitor is dlg.monitor and dlg.monitor.on and dlg.monitor.volume is w.volume
    dlg.reject()
    pump(qapp, 0.2)
    assert not dlg.monitor.on and w.cfg["record"]["monitor"] is True
    w.close()
