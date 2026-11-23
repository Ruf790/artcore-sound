"""The window in 0.4: M / R (also while playing), the marker list (double click, rename, Del), dragging a marker's
flag, a double click on it, Ctrl+← / →, the selection snapping to a marker, markers saved with the file, the view
modes with the spectrogram's tiles, the spectrum panel (the selection, then the live spectrum while playing),
Tools → Spectrum (CSV), the spectrogram settings, regions saved as files, the Record window (null input) into a new
file and into the current one."""
import os

import numpy as np
from conftest import pump
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from sound import config, media
from sound.waveview import GUTTER_W, RULER_H, TIME_H


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


def _open(qapp, w, *paths):
    n = len(w.docs) + len(paths)
    w.open_paths(list(paths))
    assert pump(qapp, 30, lambda: len(w.docs) == n and w.job is None)


def _close(w):
    for d in w.docs:
        d.mark_saved()
    w.close()


def _key(widget, key, mods=Qt.NoModifier, text=""):
    for kind in (QEvent.ShortcutOverride, QEvent.KeyPress):
        ev = QKeyEvent(kind, key, mods, text)
        QApplication.sendEvent(widget, ev)
        if kind == QEvent.ShortcutOverride and ev.isAccepted():
            continue


def _mouse(widget, kind, x, y, button=Qt.LeftButton):
    ev = QMouseEvent(kind, QPointF(x, y), widget.mapToGlobal(QPointF(x, y)), button,
                     button if kind not in (QEvent.MouseButtonRelease,) else Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(widget, ev)


def test_markers_in_the_window(qapp, session, data_dir, media_dir, tmp_path, monkeypatch):
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
    d = w.doc
    d.cursor = 44100
    w.mk_actions["add"].trigger()                                  # M
    assert [(m.name, m.pos) for m in d.markers] == [("Метка 1", 44100)]
    assert not w.mk_actions["region"].isEnabled()                   # R needs a selection
    d.sel = (22050, 66150)
    w._on_selection()
    w.mk_actions["region"].trigger()                                # R
    assert any(m.end == 66150 and m.name == "Регион 1" for m in d.markers)
    assert w.mlist.topLevelItemCount() == 2
    assert [w.mlist.topLevelItem(i).text(0) for i in range(2)] == ["Регион 1", "Метка 1"]     # by time
    assert w.mlist.topLevelItem(0).text(3) == "0:00:01.000"
    # double click in the list: a region → selected, a marker → the cursor there
    d.sel = None
    w.mlist.itemDoubleClicked.emit(w.mlist.topLevelItem(0), 0)
    assert d.sel == (22050, 66150)
    w.mlist.itemDoubleClicked.emit(w.mlist.topLevelItem(1), 0)
    assert d.sel is None and d.cursor == 44100
    # rename in the list, Del in the list
    w.mlist.topLevelItem(1).setText(0, "Припев")
    assert any(m.name == "Припев" for m in d.markers) and d.undo_label() == "rename marker"
    assert w.a_undo.text().endswith("Переименование метки")
    w.mlist.setFocus()
    w.mlist.clearSelection()
    w.mlist.topLevelItem(1).setSelected(True)
    n = d.frames
    _key(w.mlist, Qt.Key_Delete)
    assert len(d.markers) == 1 and d.frames == n                    # the marker, not the sound
    w.undo()
    assert len(d.markers) == 2
    # Ctrl+→ / Ctrl+←
    d.cursor = 0
    w.mk_actions["next"].trigger()
    assert d.cursor == 22050
    w.mk_actions["next"].trigger()
    assert d.cursor == 44100
    w.mk_actions["prev"].trigger()
    assert d.cursor == 22050
    # drag the marker's flag with the mouse
    v = w.wave
    mk = next(m for m in d.markers if m.end is None)
    x0 = v.x_of(mk.pos) + 3
    y = TIME_H + 6
    _mouse(v, QEvent.MouseButtonPress, x0, y)
    _mouse(v, QEvent.MouseMove, x0 + 40, y, Qt.NoButton)
    _mouse(v, QEvent.MouseMove, x0 + 80, y, Qt.NoButton)
    _mouse(v, QEvent.MouseButtonRelease, x0 + 80, y)
    moved = d.marker(mk.id)
    assert abs(moved.pos - (mk.pos + 80 * v.spp())) <= 2 * v.spp() and d.undo_label() == "move marker"
    # a double click on the flag renames it
    asked = {}

    def fake_text(*a, **k):
        asked["old"] = a[4] if len(a) > 4 else ""
        return "Куплет", True
    monkeypatch.setattr("sound.window.QInputDialog.getText", fake_text)
    xm = v.x_of(moved.pos) + 3
    ev = QMouseEvent(QEvent.MouseButtonDblClick, QPointF(xm, y), v.mapToGlobal(QPointF(xm, y)), Qt.LeftButton,
                     Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(v, ev)
    assert asked["old"] == "Припев" and d.marker(mk.id).name == "Куплет"
    # the selection snaps to a marker
    reg = next(m for m in d.markers if m.end is not None)
    xa = GUTTER_W + 5
    xb = v.x_of(reg.end) + 3                                         # 3 px past the region's end
    _mouse(v, QEvent.MouseButtonPress, xa, RULER_H + 60)
    _mouse(v, QEvent.MouseMove, (xa + xb) / 2, RULER_H + 60, Qt.NoButton)
    _mouse(v, QEvent.MouseMove, xb, RULER_H + 60, Qt.NoButton)
    _mouse(v, QEvent.MouseButtonRelease, xb, RULER_H + 60)
    assert d.sel[1] == reg.end
    # saved with the file and read back
    out = str(tmp_path / "with_markers.wav")
    w._save_to(d, out, "wav", "16", wait=True)
    d2 = media.open_file(out, session.folder())
    assert sorted((m.name, m.pos, m.end) for m in d2.markers) == sorted((m.name, m.pos, m.end) for m in d.markers)
    # export / import a list, delete all
    w.export_markers(str(tmp_path / "list.csv"))
    w.delete_all_markers()
    assert d.markers == ()
    w.import_markers(str(tmp_path / "list.csv"))
    assert len(d.markers) == 2
    # regions as separate files
    d.add_region(0, 44100, "a/b")
    paths = w.save_regions(str(tmp_path / "regions"), "flac", "16", wait=True)
    assert [os.path.basename(p) for p in paths] == ["01 a_b.flac", "02 Регион 1.flac"]
    info = media.ff.probe(paths[1])
    assert abs(info.duration - 1.0) < 0.01
    _close(w)


def test_marker_while_playing(qapp, session, data_dir, media_dir):
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
    w.play()
    pump(qapp, 0.6)
    w.add_marker()
    assert w.player.state == "play"                                 # playback goes on
    pos = w.doc.markers[0].pos
    assert 0.3 * 44100 < pos < 1.2 * 44100
    w.stop_play()
    _close(w)


def test_view_modes_and_spectrogram(qapp, session, data_dir, media_dir):
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "film.ac3"))
    assert w.wave.mode == "wave"
    w.mode_actions["spec"].trigger()
    assert w.wave.mode == "spec" and w.tb["mode_spec"].isChecked() and w.cfg["view_mode"] == "spec"
    w.wave.repaint()
    assert pump(qapp, 20, lambda: w.wave.engine is not None and w.wave.sg_pending() == 0 and w.wave._sg_ready > 0)
    pump(qapp, 0.2)
    img = w.wave.grab().toImage()
    v = w.wave
    lanes = v.lanes_rect()
    # the 440 Hz tone shows as a bright line in the L lane (log scale): find its row
    geo = v.lane_geom(lanes.height(), w.doc.channels)
    top, lh = geo[0]
    from sound import spectrogram as sg
    y = int(RULER_H + top + lh - float(sg.y_frac(440, 48000, "log")) * lh)
    x = int(GUTTER_W + 200)
    bright = img.pixelColor(x, y).lightness()
    dark = img.pixelColor(x, int(RULER_H + top + lh * 0.1)).lightness()
    assert bright > dark + 60
    w.tb["mode_both"].click()
    assert w.wave.mode == "both" and w.mode_actions["both"].isChecked()
    w.wave.repaint()
    assert pump(qapp, 20, lambda: w.wave.sg_pending() == 0)
    n_tiles = len(w.wave.engine.cache)
    w.a_sg_mode.trigger()                                            # F7: off
    assert w.wave.mode == "wave"
    # the settings: a palette change recomputes nothing
    from sound.anaui import SpectrogramDialog
    w.set_view_mode("spec")
    w.wave.repaint()
    pump(qapp, 0.3)
    computed = w.wave.engine.computed
    dlg = SpectrogramDialog(w)
    dlg.pal_dark.setCurrentIndex(dlg.pal_dark.findData("fire"))
    dlg.lo.setValue(-80)
    w.wave.repaint()
    pump(qapp, 0.3)
    assert w.wave.engine.computed == computed and len(w.wave.engine.cache) >= n_tiles // 2
    dlg.fft.setCurrentIndex(dlg.fft.findData(4096))                  # the FFT size: computed anew
    w.wave.repaint()
    assert pump(qapp, 20, lambda: w.wave.engine.computed > computed and w.wave.sg_pending() == 0)
    dlg.accept()
    assert w.cfg["spectrogram"]["fft"] == 4096 and w.cfg["spectrogram"]["palette_dark"] == "fire"
    _close(w)


def test_spectrum_panel_and_window(qapp, session, data_dir, media_dir, tmp_path):
    w = _win(qapp, session, spectrum_panel_on=True)
    _open(qapp, w, os.path.join(media_dir, "mono24.wav"))            # 1 kHz
    p = w.spec_panel
    assert p.isVisible()
    w.doc.sel = (0, 48000)
    w._on_selection()
    assert pump(qapp, 10, lambda: p.plot.res is not None and not p.runner.busy)
    f, v, _c = p.plot.peak()
    assert abs(f - 1000) < 3 and not p.plot.live
    w.play()
    assert pump(qapp, 5, lambda: p.plot.live)
    f, v, _c = p.plot.peak()
    assert abs(f - 1000) < 5
    w.stop_play()
    assert pump(qapp, 5, lambda: not p.plot.live and p.plot.res is not None)
    w.a_spec_panel.trigger()                                         # F6: hidden
    assert not p.isVisible() and not w.cfg["spectrum_panel_on"]
    w.spectrum_dialog()
    dlg = w.spectrum_dlg
    assert pump(qapp, 10, lambda: dlg.result is not None)
    txt = dlg.peak_l.text()
    assert ("999.9 Hz" in txt or "1 000.0 Hz" in txt) and "B5 +21 ct" in txt
    out = str(tmp_path / "s.csv")
    assert dlg.save_csv(out)
    lines = open(out, encoding="utf-8").read().splitlines()
    assert lines[0].startswith("Frequency (Hz),M (dB)") and len(lines) > 1000
    dlg.copy()
    assert QApplication.clipboard().text().startswith("Frequency (Hz)\tM (dB)")
    dlg.close()
    _close(w)


def test_record_window(qapp, session, data_dir, media_dir, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    w = _win(qapp, session)
    assert w.t_rec.isEnabled()
    w.t_rec.click()                                                  # opens the window, monitoring
    dlg = w.rec_dlg
    assert dlg is not None and dlg.rec.state == "ready"
    assert pump(qapp, 2, lambda: dlg.meters.max_db.max() > -13)      # the null tone at −12 dBFS
    w.t_rec.click()                                                  # Record
    assert dlg.rec.state == "rec"
    pump(qapp, 0.6)
    dlg.marker()
    pump(qapp, 0.5)
    w.t_rec.click()                                                  # Stop → a new document
    assert dlg.rec.state == "ready"
    assert len(w.docs) == 1 and w.doc.name == "Запись 1" and w.doc.dirty
    d = w.doc
    assert 0.9 * 48000 < d.frames < 1.4 * 48000 and len(d.markers) == 1
    # into the current file at the cursor: one undo step
    d.cursor = 24000
    d.sel = None
    dlg.t_ins.setChecked(True)
    assert not dlg.chans.isEnabled() and dlg.rec.channels == d.channels
    n0 = d.frames
    dlg.start()
    pump(qapp, 0.5)
    dlg.stop()
    assert d.frames > n0 + 0.3 * 48000 and d.undo_label() == "record"
    d.undo()
    assert d.frames == n0
    # in place of the selection
    d.sel = (0, 24000)
    w._on_selection()
    dlg._target_changed()
    dlg.t_rep.setChecked(True)
    dlg.start()
    pump(qapp, 0.4)
    dlg.stop()
    assert d.undo_label() == "record" and d.frames != n0
    dlg.close()
    pump(qapp, 0.1)
    assert w.rec_dlg is None
    _close(w)


def test_record_during_close_is_kept(qapp, session, data_dir, monkeypatch):
    asked = []
    monkeypatch.setattr("sound.window.MainWindow._ask_save", lambda self, d, i: asked.append(d.name) or True)
    w = _win(qapp, session)
    w.record_toggle()
    w.rec_dlg.start()
    pump(qapp, 0.3)
    w.close()
    assert asked == ["Запись 1"]
    assert np.isfinite(0.0)


def test_strings_have_unique_keys():
    """A key written twice in i18n.py silently replaces the first string (0.3 showed «clicks removed» after removing
    the DC offset)."""
    import ast
    import sound.i18n as i18n
    tree = ast.parse(open(i18n.__file__, encoding="utf-8").read())
    keys = [k.value for node in ast.walk(tree) if isinstance(node, ast.Dict) for k in node.keys
            if isinstance(k, ast.Constant)]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    assert dup == []
    i18n.set_lang("ru")
    assert "смещение" in i18n.t("dc_done", "x") and "щелчков" in i18n.t("declick_done", 3)
