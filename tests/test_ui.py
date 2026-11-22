"""The window: opening (also by dropping files), selecting with the mouse, editing through the menus and the tool bar,
undo, processing, saving, playback with the moving cursor, the time fields, zoom, the strings."""
import os
import re

import numpy as np
from conftest import pump
from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from sound import config
from sound.waveview import GUTTER_W, RULER_H

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _win(qapp, session, **over):
    from sound.window import MainWindow
    cfg = config.load()
    cfg.update(check_updates=False, geometry="")
    cfg.update(over)
    w = MainWindow(cfg, session)
    w.resize(1400, 860)
    w.show()
    pump(qapp, 0.1)
    return w


def _open(qapp, w, *paths):
    n = len(w.docs) + len(paths)
    w.open_paths(list(paths))
    assert pump(qapp, 30, lambda: len(w.docs) == n and w.job is None)


def _mouse(widget, kind, x, y, button=Qt.LeftButton, mods=Qt.NoModifier):
    ev = QMouseEvent(kind, QPointF(x, y), widget.mapToGlobal(QPointF(x, y)), button,
                     button if kind != QEvent.MouseButtonRelease else Qt.NoButton, mods)
    QApplication.sendEvent(widget, ev)


def _drag(w, x1, x2, y=None, mods=Qt.NoModifier):
    v = w.wave
    y = y if y is not None else RULER_H + 80
    _mouse(v, QEvent.MouseButtonPress, x1, y, mods=mods)
    _mouse(v, QEvent.MouseMove, (x1 + x2) / 2, y, Qt.NoButton)
    _mouse(v, QEvent.MouseMove, x2, y, Qt.NoButton)
    _mouse(v, QEvent.MouseButtonRelease, x2, y)


def _select(w, a, b):
    w.doc.sel = (a, b)
    w._on_selection()


def _close(w):
    for d in w.docs:
        d.mark_saved()
    w.close()


def test_empty_window(qapp, session, data_dir):
    w = _win(qapp, session)
    assert w.doc is None and not w.tabs.isVisible()
    assert not w.a_save.isEnabled() and not w.a_cut.isEnabled() and not w.tb["normalize"].isEnabled()
    menus = [a.text() for a in w.menuBar().actions()]
    assert menus == ["ArtCore Sound", "Файл", "Правка", "Вид", "Обработка", "Воспроизведение"]
    assert "Справка" not in menus
    img = w.grab().toImage()
    assert img.width() > 1000
    w.play_toggle()                                   # nothing to play: no error
    _close(w)


def test_open_select_edit_undo(qapp, session, data_dir, media_dir):
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"), os.path.join(media_dir, "film.ac3"))
    assert w.tabs.count() == 2 and w.doc.name == "film.ac3"
    assert w.meters.labels == ["L", "R", "C", "LFE", "Ls", "Rs"]
    assert "48 000" in w.fmt_label.text() and "5.1" in w.fmt_label.text()
    w.tabs.setCurrentIndex(0)
    d = w.doc
    assert d.name == "stereo16.wav" and w.meters.labels == ["L", "R"]
    v = w.wave
    # the same file again: no second tab
    w.open_paths([os.path.join(media_dir, "stereo16.wav")])
    pump(qapp, 0.3)
    assert len(w.docs) == 2
    # select with the mouse, from a quarter to the middle of the view
    lw = v.lane_width()
    _drag(w, GUTTER_W + lw * 0.25, GUTTER_W + lw * 0.5)
    a, b = d.sel
    assert abs(a - d.frames * 0.25) < d.frames * 0.01 and abs(b - d.frames * 0.5) < d.frames * 0.01
    assert w.f_len.text() == w.f_len.text() and w.f_start.text().startswith("0:00:00.7")
    assert w.a_cut.isEnabled() and w.tb["cut"].isEnabled()
    # shift-click extends the selection
    _mouse(v, QEvent.MouseButtonPress, GUTTER_W + lw * 0.75, RULER_H + 50, mods=Qt.ShiftModifier)
    _mouse(v, QEvent.MouseButtonRelease, GUTTER_W + lw * 0.75, RULER_H + 50, mods=Qt.ShiftModifier)
    assert d.sel[0] == a and abs(d.sel[1] - d.frames * 0.75) < d.frames * 0.01
    n0 = d.frames
    x0 = d.read(0, n0)
    sel = d.sel
    w.a_cut.trigger()
    assert d.frames == n0 - (sel[1] - sel[0]) and d.dirty and "•" in w.windowTitle()
    w.a_paste.trigger()
    assert d.frames == n0
    np.testing.assert_array_equal(d.read(0, n0), x0)
    w.a_undo.trigger()
    w.a_undo.trigger()
    assert d.frames == n0 and not d.dirty and d.sel == sel
    assert w.a_redo.text().startswith("Повторить: Вырезать")
    w.a_redo.trigger()
    assert d.frames == n0 - (sel[1] - sel[0])
    w.a_undo.trigger()
    # silence, trim, delete through the tool bar buttons
    _select(w, 1000, 2000)
    w.tb["silence"].click()
    assert np.abs(d.read(1000, 1000)).max() == 0
    w.tb["trim"].click()
    assert d.frames == 1000
    w.tb["undo"].click()
    w.tb["undo"].click()
    assert d.frames == n0 and not d.dirty
    # the history menu lists the steps
    w._fill_history()
    acts = w.history_menu.actions()
    assert acts[0].text().startswith("1. Открытие") and len(acts) == len(d.history)
    # select all / none, the selection fields
    w.a_sel_all.trigger()
    assert d.sel == (0, n0)
    w.f_start.setText("0:00:01.000")
    w.f_start.editingFinished.emit()
    w.f_len.setText("0.5")
    w.f_len.editingFinished.emit()
    assert d.sel == (44100, 44100 + 22050)
    w.a_sel_none.trigger()
    assert d.sel is None
    _close(w)


def test_paste_between_files_converts(qapp, session, data_dir, media_dir):
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "film.ac3"), os.path.join(media_dir, "stereo16.wav"))
    film, song = w.docs
    w.tabs.setCurrentIndex(0)
    _select(w, 0, 48000)
    w.a_copy.trigger()
    w.tabs.setCurrentIndex(1)
    song.cursor = 0
    song.sel = None
    w._on_selection()
    n = song.frames
    w.a_paste.trigger()
    assert pump(qapp, 20, lambda: w.job is None and song.frames > n)
    assert song.frames == n + 44100 and song.channels == 2 and song.sel == (0, 44100)
    # paste as a new file keeps the clip's own format
    w.a_paste_new.trigger()
    assert len(w.docs) == 3 and w.doc.channels == 6 and w.doc.frames == 48000 and w.doc.name.startswith("Без имени")
    _close(w)


def test_processing_from_the_menu(qapp, session, data_dir, media_dir, monkeypatch):
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
    d = w.doc
    from sound import dialogs
    monkeypatch.setattr(dialogs.ValueDialog, "exec", lambda self: True)
    w.normalize_dialog()                              # the whole file to −0.1 dBFS
    assert pump(qapp, 20, lambda: w.job is None and len(d.history) == 2)
    assert abs(d.peak().max() - 10 ** (-0.1 / 20)) < 1e-4
    _select(w, 0, 44100)
    w.fade("fade_in")
    assert pump(qapp, 20, lambda: w.job is None and len(d.history) == 3)
    assert np.abs(d.read(0, 100)).max() < 0.01 and d.sel == (0, 44100)
    w.simple_op("reverse")
    assert pump(qapp, 20, lambda: w.job is None and len(d.history) == 4)
    assert w.a_undo.text() == "Отменить: Реверс"
    w.resample_dialog()                               # 44.1 → 48 kHz
    assert pump(qapp, 20, lambda: w.job is None and d.rate == 48000)
    assert abs(d.frames - 3 * 48000) < 64 and "48 000" in w.fmt_label.text()
    w.channels_dialog()                               # stereo → mono
    assert pump(qapp, 20, lambda: w.job is None and d.channels == 1)
    assert w.meters.labels == ["M"] and d.labels == ("M",)
    w.a_undo.trigger()
    w.a_undo.trigger()
    assert d.rate == 44100 and d.channels == 2 and w.meters.labels == ["L", "R"]
    _close(w)


def test_save_and_save_as(qapp, session, data_dir, media_dir, tmp_path, monkeypatch):
    import shutil
    src = str(tmp_path / "copy.wav")
    shutil.copy(os.path.join(media_dir, "stereo16.wav"), src)
    w = _win(qapp, session)
    _open(qapp, w, src)
    d = w.doc
    _select(w, 0, 44100)
    w.a_delete.trigger()
    assert d.dirty
    assert w.save(wait=True)                         # WAV 16 bit as it came
    assert not d.dirty and "•" not in w.windowTitle()
    from sound import ff
    tr = ff.probe(src).tracks[0]
    assert tr.codec == "pcm_s16le" and abs(tr.duration - 2.0) < 0.01
    from sound import dialogs
    out = str(tmp_path / "out.flac")
    monkeypatch.setattr(dialogs.SaveDialog, "exec", lambda self: (self.name.setText(out), self.fmt.setCurrentIndex(
        self.fmt.findData("flac")), self.opt.setCurrentIndex(self.opt.findData("24")), True)[-1])
    assert w.save_as(wait=True)
    assert d.path == out and d.name == "out.flac" and d.save_format == "flac" and w.tabs.tabText(0) == "out.flac"
    assert ff.probe(out).tracks[0].bits == 24
    assert w.cfg["recent"][0] == out and config.load()["save_format"] == "flac"
    _close(w)


def test_unsaved_question_on_close(qapp, session, data_dir, media_dir, monkeypatch):
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
    w.doc.silence(0, 100)
    asked = []

    def fake_exec(box):
        asked.append(box.text())
        box.clickedButton = lambda: [b for b in box.buttons() if box.buttonRole(b) == QMessageBox.RejectRole][0]
        return 0
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    assert not w.close_doc(0)                       # Cancel: the file stays open
    assert asked and "stereo16.wav" in asked[0] and len(w.docs) == 1

    def dont_save(box):
        box.clickedButton = lambda: [b for b in box.buttons() if box.buttonRole(b) == QMessageBox.DestructiveRole][0]
        return 0
    monkeypatch.setattr(QMessageBox, "exec", dont_save)
    folder = w.doc.folder
    assert w.close_doc(0) and not w.docs and not os.path.exists(folder)   # its working files are gone
    w.close()


def test_playback_cursor_and_meters(qapp, session, data_dir, media_dir):
    w = _win(qapp, session, return_cursor=True)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
    d = w.doc
    _select(w, 44100, 66150)
    d.cursor = 44100
    w.a_play.trigger()
    assert w.player.state == "play"
    assert pump(qapp, 0.3)
    assert w.wave.play_pos is not None and 44100 < w.wave.play_pos < 66150
    assert w.counter.text() != "0:00:01.000"
    assert w.meters.level.max() > -12
    assert pump(qapp, 3, lambda: w.player.state == "stop")       # the selection played out
    assert d.cursor == 44100 and w.wave.play_pos is None        # the cursor came back
    # loop: plays on until stopped
    w._toggle_loop()
    w.a_play.trigger()
    pump(qapp, 0.9)
    assert w.player.state == "play"
    w.play_toggle()
    assert w.player.state == "stop"
    w._toggle_loop()
    _close(w)


def test_zoom_scroll_and_wheel(qapp, session, data_dir, media_dir):
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
    v, d = w.wave, w.doc
    assert v.spp() == v.fit_spp() and v.visible()[0] == 0
    v.zoom(0.25)
    assert abs(v.spp() - v.fit_spp() / 4) < 1e-6
    w.hbar.setValue(w.hbar.maximum())
    assert abs(v.visible()[1] - d.frames) < v.spp() * 2
    w.a_zoom_all.trigger()
    assert v.spp() == v.fit_spp()
    d.cursor = 30000
    w.a_zoom_smp.trigger()
    assert v.spp() == 1.0
    w.a_zoom_in.trigger()
    w.a_zoom_in.trigger()
    assert v.spp() == 0.25
    for _ in range(10):
        w.a_zoom_in.trigger()
    assert v.spp() == 1 / 32                           # 32 pixels a sample at most
    pos = QPointF(GUTTER_W + 300, RULER_H + 40)
    ev = QWheelEvent(pos, v.mapToGlobal(pos), QPoint(), QPoint(0, -120 * 6), Qt.NoButton, Qt.ControlModifier,
                     Qt.NoScrollPhase, False)
    QApplication.sendEvent(v, ev)
    assert v.spp() > 1 / 32
    a = v.visible()[0]
    ev = QWheelEvent(pos, v.mapToGlobal(pos), QPoint(), QPoint(0, -120), Qt.NoButton, Qt.NoModifier,
                     Qt.NoScrollPhase, False)
    QApplication.sendEvent(v, ev)
    assert v.visible()[0] > a
    img = v.grab().toImage()                           # the sample-level drawing works
    assert img.width() == v.width()
    _close(w)


def test_drop_files_on_the_window(qapp, session, data_dir, media_dir):
    w = _win(qapp, session)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(os.path.join(media_dir, "song.mp3"))])
    ev = QDropEvent(QPointF(400, 300), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    w.wave.dropEvent(ev)                     # (Qt delivers a drop only inside a real drag, so call the handler)
    assert pump(qapp, 20, lambda: len(w.docs) == 1 and w.job is None)
    assert w.doc.name == "song.mp3"
    _close(w)


def test_film_asks_for_the_track(qapp, session, data_dir, media_dir, monkeypatch):
    from sound import dialogs
    monkeypatch.setattr(dialogs.TrackDialog, "exec", lambda self: (self.list.setCurrentRow(1), True)[-1])
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "Фильм (2024).mkv"))
    assert w.doc.channels == 2 and w.doc.save_format == ""
    _close(w)


def test_light_theme_and_english(qapp, session, data_dir, media_dir):
    from sound import i18n, style
    i18n.set_lang("en")
    try:
        w = _win(qapp, session)
        assert [a.text() for a in w.menuBar().actions()] == ["ArtCore Sound", "File", "Edit", "View", "Process",
                                                              "Transport"]
        _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
        w._set_theme("light")
        assert style.THEME == "light"
        img = w.wave.grab().toImage()
        assert img.pixelColor(GUTTER_W + 5, RULER_H + 5).lightness() > 200
        w._set_theme("dark")
        _close(w)
    finally:
        i18n.set_lang("ru")


def test_every_string_exists_in_both_languages():
    from sound.i18n import STRINGS
    for k, (ru, en) in STRINGS.items():
        assert ru and en, k
        assert ru.count("{") == en.count("{"), k
    used = set()
    for dp, _, fs in os.walk(os.path.join(ROOT, "sound")):
        for f in fs:
            if f.endswith(".py"):
                src = open(os.path.join(dp, f), encoding="utf-8").read()
                used |= set(re.findall(r'\bt\("([a-z0-9_:]+)"\s*[,)]', src))
    missing = sorted(k for k in used if k not in STRINGS)
    assert not missing, missing


def test_version_in_three_places():
    from sound import VERSION, VERSION_TUPLE
    assert VERSION == ".".join(str(x) for x in VERSION_TUPLE[:3])
    iss = open(os.path.join(ROOT, "setup.iss"), encoding="utf-8-sig").read()
    assert f'#define AppVersion   "{VERSION}"' in iss
    vi = open(os.path.join(ROOT, "version_info.txt"), encoding="utf-8").read()
    assert f"filevers={VERSION_TUPLE}" in vi and f"'ProductVersion', '{VERSION}'" in vi
