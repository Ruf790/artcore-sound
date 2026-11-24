"""Restoration in the window (2.0, block 2): the Restoration menu (the noise reduction and click removal moved there
from Effects, the new effects, the noise sample items), the hum found when its window opens and followed during the
preview, the clipping found and marked on the wave with the headroom it needs, Apply as one undo step with its count,
the de-esser's meter, the batch's Restoration section."""
import numpy as np
from conftest import ff, pump
from test_fx_ui import _close, _dlg, _open, _win

from sound import fx


def test_restoration_menu(qapp, session, data_dir):
    w = _win(qapp, session)
    menus = {a.text(): a.menu() for a in w.menuBar().actions()}
    rest = [a.objectName() for a in menus["Реставрация"].actions() if not a.isSeparator()]
    assert rest[0] == "fx_take_noise"
    for k in fx.RESTORE:
        assert "fx_" + k in rest
    effects = [a.objectName() for a in menus["Эффекты"].actions()]
    assert "fx_nr" not in effects and "fx_declick" not in effects and "fx_take_noise" not in effects
    assert "fx_eq" in effects and "fx_tempo" in effects
    titles = [a.text() for a in menus["Реставрация"].actions()]
    assert "Удаление фона (50/60 Гц)…" in titles and "Восстановление клиппинга…" in titles
    _close(w)


def _hum_file(tmp_path, f0=50.2):
    p = str(tmp_path / "hum.wav")
    ff("-f", "lavfi", "-i", f"aevalsrc='0.2*sin(2*PI*330*t)+0.03*sin(2*PI*{f0}*t)+0.02*sin(2*PI*{3 * f0}*t)"
       f"+0.01*sin(2*PI*{5 * f0}*t)':s=48000:d=4", "-c:a", "pcm_f32le", p)
    return p


def test_hum_window(qapp, session, data_dir, tmp_path):
    w = _win(qapp, session)
    _open(qapp, w, _hum_file(tmp_path))
    d = w.doc
    dlg = _dlg(w, "dehum")
    assert pump(qapp, 10, lambda: dlg.b_detect.isEnabled() and dlg.hum_label.text())
    assert "50.2" in dlg.hum_label.text() and dlg.hum_result["base"] == 50.0
    dlg.b_preview.setChecked(True)
    assert pump(qapp, 5, lambda: "50.2" in dlg.hum_now.text())
    dlg.b_preview.setChecked(False)
    dlg.accept()
    w.apply_effect("dehum", dlg.result_values())
    assert pump(qapp, 30, lambda: w.job is None and len(d.history) == 2)
    x = d.read(48000, 2 * 48000)[:, 0].astype(np.float64)
    t = np.arange(len(x)) / 48000 + 1.0
    hum = np.sin(2 * np.pi * 50.2 * t)
    assert abs(np.dot(x, hum) / np.dot(hum, hum)) < 0.03 * 0.05             # −26 dB and more
    _close(w)


def test_clipping_window(qapp, session, data_dir, tmp_path):
    p = str(tmp_path / "clip.wav")
    ff("-f", "lavfi", "-i", "aevalsrc='clip(0.9*sin(2*PI*220*t)+0.4*sin(2*PI*330*t), -0.7, 0.7)':s=48000:d=1",
       "-c:a", "pcm_f32le", p)
    w = _win(qapp, session)
    _open(qapp, w, p)
    d = w.doc
    dlg = _dlg(w, "declip")
    assert not dlg.rows["threshold"].isEnabled()
    dlg.set_values({**dlg.values, "auto": False})
    assert dlg.rows["threshold"].isEnabled()
    dlg.set_values({**dlg.values, "auto": True, "headroom": 0.0})
    dlg.b_find.click()
    assert pump(qapp, 20, lambda: dlg.b_find.isEnabled() and dlg.marks)
    res = dlg.scan_result
    assert res["runs"] == len(dlg.marks) > 100 and w.wave.marks() == dlg.marks
    assert "dBFS" in dlg.found.text() and "понизьте минимум" in dlg.found.text()   # 0 dB headroom is not enough
    vals = dlg.result_values()
    dlg.reject()
    w.apply_effect("declip", {**vals, "headroom": res["need_db"]})
    assert pump(qapp, 30, lambda: w.job is None and len(d.history) == 2)
    assert str(res["runs"]) in w.statusBar().currentMessage()
    assert np.abs(d.read(0, d.frames)).max() <= 10 ** (-0.1 / 20) + 1e-4
    _close(w)


def test_deesser_meter_and_crackle_window(qapp, session, data_dir, media_dir):
    import os
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
    dlg = _dlg(w, "deess")
    assert dlg.gr is not None
    dlg.set_values({**dlg.values, "freq": 2000.0, "threshold": -60.0})
    dlg.b_preview.setChecked(True)
    assert pump(qapp, 3, lambda: dlg.gr.hold > 1.0)
    dlg.reject()
    for k in ("decrackle", "nr_auto"):
        dlg = _dlg(w, k)
        assert dlg.findChildren(type(dlg.what))                          # the window builds, with its note
        dlg.reject()
    _close(w)


def test_batch_has_a_restoration_section(qapp, session, data_dir):
    from sound import batchui
    w = _win(qapp, session)
    bw = batchui.BatchWindow(w)
    texts = [a.text() for a in bw.b_add.menu().actions()]
    i = texts.index("Реставрация")
    assert "Удаление треска" in texts[i:] and "Шумоподавление" in texts[i:] and "Компрессор" in texts[:i]
    bw.close()
    _close(w)
