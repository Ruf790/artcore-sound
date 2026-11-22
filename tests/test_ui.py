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
    assert menus == ["ArtCore Sound", "Файл", "Правка", "Вид", "Обработка", "Инструменты", "Воспроизведение"]
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
    monkeypatch.setattr(dialogs.NormalizeDialog, "exec", lambda self: True)
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
                                                              "Tools", "Transport"]
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


# ── 0.2: the status bar, Tools → Statistics, the new processing ─────────
def _stats_ready(w):
    return "…" not in w.st_stats.text() and w.st_stats.text() != ""


def test_status_bar_measures_in_the_background(qapp, session, data_dir, loud_dir):
    from sound import analysis
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(loud_dir, "pink.wav"))
    d = w.doc
    txt = w.st_stats.text()
    assert "Пик" in txt and "dBFS" in txt and "…" in txt          # the peak at once, the rest is being measured
    assert pump(qapp, 20, lambda: _stats_ready(w))
    st = analysis.analyze(d, loudness=True, true_peak=False)
    from sound.dialogs import fmt_db
    assert f"{fmt_db(st.integrated)} LUFS" in w.st_stats.text() and "клиппинг 0" in w.st_stats.text()
    whole = w.st_stats.text()
    # a selection: the peak of it at once, the rest after a moment; the result is cached
    _select(w, 44100 * 5, 44100 * 15)
    assert "…" in w.st_stats.text()
    assert pump(qapp, 20, lambda: _stats_ready(w)) and w.st_stats.text() != whole
    sel_text = w.st_stats.text()
    w.select_none()
    assert w.st_stats.text() == whole                                 # from the cache, no waiting
    _select(w, 44100 * 5, 44100 * 15)
    assert w.st_stats.text() == sel_text
    # moving the selection quickly measures only the last one
    for i in range(10):
        _select(w, 1000 * i, 44100 * 3 + 1000 * i)
    assert pump(qapp, 20, lambda: _stats_ready(w))
    assert w.stats_key(d, 9000, 44100 * 3 + 9000) in w.stats_cache
    assert w.stats_key(d, 4000, 44100 * 3 + 4000) not in w.stats_cache
    # an edit: measured again
    w.silence()
    assert "…" in w.st_stats.text()
    assert pump(qapp, 20, lambda: _stats_ready(w))
    w.close_doc(0) if not w.doc.dirty else (w.doc.mark_saved(), w.close_doc(0))
    assert w.st_stats.text() == ""                                    # no file: nothing shown
    _close(w)


def test_status_bar_shows_clipping(qapp, session, data_dir, loud_dir):
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(loud_dir, "clipped.wav"))
    assert pump(qapp, 20, lambda: _stats_ready(w))
    assert "клиппинг" in w.st_stats.text() and "клиппинг 0" not in w.st_stats.text()
    _close(w)


def test_statistics_dialog(qapp, session, data_dir, loud_dir):
    w = _win(qapp, session)
    assert [a.text() for a in w.menuBar().actions()][5] == "Инструменты"
    _open(qapp, w, os.path.join(loud_dir, "dc_tone.wav"))
    assert w.a_stats.isEnabled() and w.a_stats.shortcut().toString() == "Ctrl+I"
    w.a_stats.trigger()
    dlg = w.stats_dlg
    assert dlg.isVisible()
    assert pump(qapp, 30, lambda: dlg.prog is None and "LUFS" in dlg.loud["st_integrated"].text())
    v = dlg.values()
    assert "весь файл" in dlg.what.text()
    assert [dlg.table.horizontalHeaderItem(i).text() for i in range(3)] == ["L", "R", "Все каналы"]
    assert v[("st_dc", "L")] == "10.000" and v[("st_dc", "R")] == "−5.000"
    assert v[("st_peak", "L")] == "−7.96" and v[("st_peak", "R")] == "−12.04"            # 0.1 + 0.3, −0.05 − 0.2
    assert float(v[("st_tp", "R")].replace("−", "-")) >= -12.04
    assert v[("st_clipped", "Все каналы")] == "0"
    assert v["st_lra"].endswith("LU")
    whole = v["st_integrated"]
    # a selection and «Refresh»
    _select(w, 0, 44100)
    dlg.refresh_b.click()
    assert pump(qapp, 30, lambda: dlg.prog is None and "выделение" in dlg.what.text())
    assert "0:00:01.000" in dlg.what.text()
    assert dlg.values()["st_integrated"].endswith("LUFS")
    # the result went into the cache: the status bar uses it at once
    assert w.stats_key(w.doc, 0, 44100) in w.stats_cache
    # Cancel stops a long measuring
    _select(w, 0, w.doc.frames - 1)
    dlg.refresh()
    assert dlg.cancel_b.isVisible() or dlg.prog is None
    dlg.cancel_b.click()
    assert pump(qapp, 10, lambda: dlg.prog is None and dlg.refresh_b.isEnabled())
    dlg.close()
    _close(w)
    assert whole


def test_normalize_lufs_from_the_menu(qapp, session, data_dir, loud_dir, monkeypatch):
    from sound import analysis, dialogs
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(loud_dir, "pink.wav"))
    d = w.doc
    monkeypatch.setattr(dialogs.NormalizeDialog, "exec", lambda self: True)
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    w.cfg["normalize"] = {"mode": "loudness", "target": -16.0, "ceiling": -1.0}
    w.normalize_dialog()
    assert pump(qapp, 30, lambda: w.job is None and len(d.history) == 2)
    assert abs(analysis.analyze(d).integrated + 16.0) < 0.01 and not told
    assert w.a_undo.text() == "Отменить: Нормализация до −16.0 LUFS"
    # a custom target that the ceiling holds back: said in a message
    w.cfg["normalize"] = {"mode": "loudness", "target": -5.0, "ceiling": -2.0}
    w.normalize_dialog()
    assert pump(qapp, 30, lambda: w.job is None and len(d.history) == 3)
    assert told and "потолок" in told[0]
    assert abs(analysis.to_db(analysis.analyze(d).true_peak) + 2.0) < 0.01
    # the dialog itself: the presets, a custom target, the peak mode
    dlg = dialogs.NormalizeDialog({"normalize": {"mode": "loudness", "target": -14.0}})
    assert dlg.mode() == "loudness" and dlg.lufs_target() == -14.0 and not dlg.custom.isEnabled()
    dlg.target.setCurrentIndex(dlg.target.count() - 1)
    dlg.custom.setValue(-18.5)
    assert dlg.custom.isEnabled() and dlg.settings()["target"] == -18.5
    dlg.by_peak.setChecked(True)
    assert not dlg.target.isEnabled() and dlg.peak.isEnabled() and dlg.mode() == "peak"
    dlg.tp.setChecked(True)
    assert dlg.peak.suffix() == " dBTP"
    dlg.deleteLater()
    _close(w)


def test_dc_trim_silence_and_mono_from_the_menu(qapp, session, data_dir, loud_dir, monkeypatch):
    from sound import analysis, dialogs
    w = _win(qapp, session)
    monkeypatch.setattr(dialogs.ValueDialog, "exec", lambda self: True)
    _open(qapp, w, os.path.join(loud_dir, "dc_tone.wav"))
    d = w.doc
    names = [a.objectName() for a in w.proc_actions]
    assert {"p_dc", "p_trim_silence", "p_to_mono", "p_to_stereo"} <= set(names)
    assert w.a_to_mono.isEnabled() and not w.a_to_stereo.isEnabled()
    w.remove_dc()
    assert pump(qapp, 20, lambda: w.job is None and len(d.history) == 2)
    assert all(abs(c.dc) < 1e-6 for c in analysis.analyze(d, loudness=False, true_peak=False).channels)
    assert w.a_undo.text() == "Отменить: Убрать DC offset"
    w.cfg["to_mono"] = "left"
    w.to_mono_dialog()
    assert pump(qapp, 20, lambda: w.job is None and d.channels == 1)
    assert d.labels == ("M",) and w.meters.labels == ["M"]
    assert not w.a_to_mono.isEnabled() and w.a_to_stereo.isEnabled()
    w.to_stereo()
    assert pump(qapp, 20, lambda: w.job is None and d.channels == 2)
    w.a_undo.trigger()
    w.a_undo.trigger()
    assert d.channels == 2 and len(d.history) == 4 and d.pos == 1
    # trim silence: a tone between silences, the whole file, no sound written
    _open(qapp, w, os.path.join(loud_dir, "gap.wav"))
    e = w.doc
    srcs = e.sources()
    w.cfg["trim_silence"] = {"threshold": -60.0, "margin": 10}
    w.trim_silence_dialog()
    assert w.job is None and len(e.history) == 2 and e.sources() == srcs
    assert abs(e.frames - (2 * 44100 + 2 * 441)) <= 3
    assert w.a_undo.text() == "Отменить: Обрезать тишину в начале и конце"
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    _select(w, 0, 400)
    w.cfg["trim_silence"] = {"threshold": 0.0, "margin": 0}
    w.trim_silence_dialog()                              # everything is quieter than 0 dBFS: said, nothing cut
    assert told and len(e.history) == 2
    _close(w)
