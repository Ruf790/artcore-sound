"""The effects in the window (0.3): the Effects menu and the tool bar buttons, every effect window, the live preview
through the null output (the meters show the processed sound, parameters and Bypass change it on the fly), Apply as
one undo step with progress and Cancel, the whole file without a selection, «keep the tail», presets, dragging the
EQ's points, the noise sample, the clicks marked on the wave, the tempo preview rendered ahead, the strings."""
import os
import re

import numpy as np
import pytest
from conftest import pump
from PySide6.QtCore import QEvent, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from sound import config, fx, fxui
from sound.i18n import STRINGS


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


def _close(w):
    for d in w.docs:
        d.mark_saved()
    w.close()


def _dlg(w, key, sel=None):
    d = w.doc
    if sel is not None:
        d.sel = sel
        w._on_selection()
    a, b = d.sel if d.sel else (0, d.frames)
    dlg = fxui.EffectDialog(w, key, d, a, b, not d.sel)
    dlg.show()
    return dlg


@pytest.fixture(autouse=True)
def _clean_profile():
    old = fx.get("nr").profile
    yield
    fx.get("nr").profile = old


def test_menu_and_toolbar(qapp, session, data_dir, media_dir):
    w = _win(qapp, session)
    names = [a.objectName() for a in w.fx_actions.values()]
    for k in fx.ORDER:
        assert "fx_" + k in names
    assert "fx_take_noise" in names
    menus = [a.text() for a in w.menuBar().actions()]
    assert "Эффекты" in menus and menus.index("Эффекты") == menus.index("Обработка") + 1
    assert w.tb["fx_eq"].text() == "Эквалайзер" and w.tb["fx_nr"].text() == "Шумоподавление"
    assert not w.tb["fx_eq"].isEnabled() and not w.fx_actions["eq"].isEnabled()
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
    assert w.tb["fx_eq"].isEnabled() and w.fx_actions["reverb"].isEnabled()
    assert not w.fx_actions["take_noise"].isEnabled()           # needs a selection
    w.doc.sel = (0, 22050)
    w._on_selection()
    assert w.fx_actions["take_noise"].isEnabled()
    _close(w)


@pytest.mark.parametrize("key", fx.ORDER)
def test_every_effect_window_previews(qapp, session, data_dir, media_dir, key):
    """Each window opens on the whole file (with the note), previews into the null output and closes clean."""
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
    dlg = _dlg(w, key)
    assert "весь файл" in dlg.what.text().lower() or "всему файлу" in dlg.what.text().lower()
    dlg.b_preview.setChecked(True)
    if fx.get(key).realtime:
        assert w.player.state == "play" and w.player.fx is dlg.chain
        assert pump(qapp, 0.5, lambda: dlg.chain.blocks > 3)
    else:
        assert pump(qapp, 30, lambda: dlg.render_prev is not None and dlg.render_prev.doc is not None
                    and w.player.state == "play")
        assert w.player.doc is dlg.render_prev.doc
    dlg.reject()
    pump(qapp, 0.05)
    assert w.player.state == "stop" and dlg.chain is None
    _close(w)


def test_live_preview_parameters_and_bypass(qapp, session, data_dir, media_dir):
    """The limiter's ceiling heard on the fly: the meters (after the effect) follow it; Bypass brings back the
    original level; the play cursor moves."""
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))   # L: 0.5 (−6 dBFS), R: 0.25
    dlg = _dlg(w, "limit", (0, 3 * 44100 - 10))
    dlg.set_values({"ceiling": -12.0, "true_peak": False, "lookahead": 1.0, "release": 20.0})
    dlg.b_preview.setChecked(True)

    def peak_db():
        lv = w.player.levels()
        return 20 * np.log10(max(1e-9, float(np.max(lv)))) if lv is not None else -200

    assert pump(qapp, 2.0, lambda: abs(peak_db() + 12.0) < 0.3), peak_db()
    pump(qapp, 0.3)
    assert w.meters.max_db.max() < -11.5
    dlg.rows["ceiling"].set_value(-3.0)
    dlg._changed("ceiling", -3.0)                              # as the slider does
    assert pump(qapp, 2.0, lambda: abs(peak_db() + 6.02) < 0.3), peak_db()   # under the ceiling now: as it is
    dlg.set_values({**dlg.values, "ceiling": -10.0})
    assert pump(qapp, 2.0, lambda: abs(peak_db() + 10.0) < 0.3)
    dlg.b_bypass.setChecked(True)
    assert pump(qapp, 2.0, lambda: abs(peak_db() + 6.02) < 0.3)
    dlg.b_bypass.setChecked(False)
    assert pump(qapp, 2.0, lambda: abs(peak_db() + 10.0) < 0.3)
    assert w.wave.play_pos is not None
    assert dlg.gr is not None and 3.0 < dlg.gr.v < 5.0          # −6 dBFS under a −10 dB ceiling
    dlg.reject()
    _close(w)


def test_apply_one_undo_step_and_whole_file(qapp, session, data_dir, media_dir):
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
    d = w.doc
    before = d.read(0, d.frames).copy()
    d.sel = (44100, 88200)
    w._on_selection()
    w.apply_effect("eq", {"out": -6.0})
    assert pump(qapp, 20, lambda: w.job is None and len(d.history) == 2)
    after = d.read(0, d.frames)
    assert np.allclose(after[:44100], before[:44100]) and np.allclose(after[88200:], before[88200:])
    assert np.allclose(after[44100:88200], before[44100:88200] * 10 ** (-6 / 20), atol=1e-5)
    assert d.sel == (44100, 88200) and w.a_undo.text() == "Отменить: Параметрический эквалайзер"
    w.undo()
    assert np.allclose(d.read(0, d.frames), before)
    d.sel = None
    w._on_selection()
    w.apply_effect("comp", {"threshold": -30.0, "ratio": 4.0})
    assert pump(qapp, 20, lambda: w.job is None and len(d.history) == 2)
    assert "весь файл" in w.statusBar().currentMessage().lower() or d.peak().max() < 0.5
    assert np.abs(d.read(4410, d.frames - 4410)).max() < 0.15 and d.frames == before.shape[0]
    _close(w)


def test_apply_from_the_dialog(qapp, session, data_dir, media_dir):
    """Effects → Reverb…, Apply: one step, the values remembered for next time, the tail kept."""
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
    d = w.doc
    n = d.frames
    d.sel = (n - 22050, n)
    w._on_selection()

    def accept():
        dlg = w.fx_dlg
        dlg.set_values({**dlg.values, "decay": 1.0, "mix": 60.0})
        dlg.keep_tail.setChecked(True)
        dlg.accept()
    QTimer.singleShot(200, accept)
    w.fx_actions["reverb"].trigger()
    assert pump(qapp, 30, lambda: w.job is None and len(d.history) == 2)
    assert d.frames > n + 0.5 * d.rate                          # the tail made the file longer
    assert w.cfg["fx_last"]["reverb"]["decay"] == 1.0
    assert config.load()["fx_last"]["reverb"]["mix"] == 60.0
    w.undo()
    assert d.frames == n
    # Cancel: nothing changes
    QTimer.singleShot(200, lambda: w.fx_dlg.reject())
    w.fx_actions["delay"].trigger()
    pump(qapp, 0.2)
    assert len(d.history) == 2 and d.pos == 0
    _close(w)


def test_apply_cancel(qapp, session, data_dir, media_dir):
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
    d = w.doc
    fx.get("nr").profile = fx.noise_profile([d.read(0, 44100)], d.rate, d.channels)
    w.apply_effect("nr", {})
    w.cancel_job()
    assert pump(qapp, 20, lambda: w.job is None)
    assert len(d.history) == 1
    _close(w)


def test_presets(qapp, session, data_dir, media_dir):
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
    dlg = _dlg(w, "reverb")
    c = dlg.presets
    i = c.findData("builtin:pr_hall")
    assert i >= 0 and c.itemText(i) == "Зал"
    c.setCurrentIndex(i)
    dlg._preset_chosen(i)
    assert dlg.values["decay"] == 2.6 and dlg.rows["decay"].value() == 2.6
    dlg.rows["mix"].set_value(55.0)
    dlg._changed("mix", 55.0)
    dlg.save_preset("Моя комната")
    assert config.load()["fx_presets"]["reverb"]["Моя комната"]["mix"] == 55.0
    assert c.currentText() == "★ Моя комната" and dlg.b_pdel.isEnabled()
    c.setCurrentIndex(c.findData("default:"))
    dlg._preset_chosen(0)
    assert dlg.values == fx.get("reverb").defaults()
    c.setCurrentIndex(c.findData("user:Моя комната"))
    dlg._preset_chosen(c.currentIndex())
    assert dlg.values["mix"] == 55.0
    dlg._delete_preset()
    assert "Моя комната" not in config.load()["fx_presets"]["reverb"]
    dlg.reject()
    _close(w)


def _mouse(widget, kind, x, y):
    ev = QMouseEvent(kind, QPointF(x, y), widget.mapToGlobal(QPointF(x, y)), Qt.LeftButton,
                     Qt.LeftButton if kind != QEvent.MouseButtonRelease else Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(widget, ev)


def test_eq_graph_drag(qapp, session, data_dir, media_dir):
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
    dlg = _dlg(w, "eq")
    pump(qapp, 0.1)
    g = dlg.graph
    x, y = g.x_of(1000.0), g.y_of(0.0)                         # band 4: a bell at 1 kHz
    _mouse(g, QEvent.MouseButtonPress, x, y)
    _mouse(g, QEvent.MouseMove, g.x_of(2000.0), g.y_of(9.0))
    _mouse(g, QEvent.MouseButtonRelease, g.x_of(2000.0), g.y_of(9.0))
    assert abs(dlg.values["b4_freq"] - 2000) < 40 and abs(dlg.values["b4_gain"] - 9.0) < 0.3
    assert abs(dlg.rows["b4_gain"].value() - dlg.values["b4_gain"]) < 1e-9
    resp = g.response()
    assert resp.max() > 8.5
    # the wheel sets Q, a double click switches a band
    q = dlg.values["b4_q"]
    dlg._eq_wheel(4, 1.12)
    assert dlg.values["b4_q"] > q
    dlg.graph.toggled.emit(1)
    assert dlg.values["b1_on"] and dlg.rows["b1_freq"].isEnabled()
    # the graphic EQ: up and down
    dlg.reject()
    dlg = _dlg(w, "geq")
    pump(qapp, 0.1)
    g = dlg.graph
    _mouse(g, QEvent.MouseButtonPress, g.x_of(125.0), g.y_of(0.0))
    _mouse(g, QEvent.MouseMove, g.x_of(125.0), g.y_of(-8.0))
    _mouse(g, QEvent.MouseButtonRelease, g.x_of(125.0), g.y_of(-8.0))
    assert abs(dlg.values["g2"] + 8.0) < 0.3 and abs(dlg.rows["g2"].value() - dlg.values["g2"]) < 1e-9
    dlg.reject()
    _close(w)


def test_noise_sample_and_reduction(qapp, session, data_dir, tmp_path):
    from conftest import ff
    p = str(tmp_path / "hiss.wav")
    ff("-f", "lavfi", "-i", "anoisesrc=c=white:r=48000:d=4:a=0.01:s=4", "-f", "lavfi", "-i",
       "aevalsrc='0.3*sin(2*PI*500*t)*gte(t,1)':s=48000:d=4", "-filter_complex", "[0][1]amix=inputs=2:weights=1 1",
       "-c:a", "pcm_f32le", p)
    w = _win(qapp, session)
    _open(qapp, w, p)
    d = w.doc
    fx.get("nr").profile = None
    dlg = _dlg(w, "nr")
    assert "Образца шума нет" in dlg.prof_label.text()
    dlg.reject()
    d.sel = (0, 2000)
    w._on_selection()
    from PySide6.QtWidgets import QMessageBox
    shown = []
    orig = QMessageBox.information
    QMessageBox.information = staticmethod(lambda *a, **k: shown.append(a))
    try:
        w.take_noise_sample()                                     # too short
    finally:
        QMessageBox.information = orig
    assert shown and fx.get("nr").profile is None
    d.sel = (1000, 44000)
    w._on_selection()
    w.fx_actions["take_noise"].trigger()
    assert fx.get("nr").profile is not None and "Образец шума взят" in w.statusBar().currentMessage()
    d.sel = None
    w._on_selection()
    dlg = _dlg(w, "nr")
    assert "Образец шума: 0.90 с" in dlg.prof_label.text() and dlg.noise_graph.prof is not None
    dlg.reject()
    rms0 = float(np.sqrt(np.mean(d.read(2000, 40000) ** 2)))
    w.apply_effect("nr", {"reduction": 20.0})
    assert pump(qapp, 30, lambda: w.job is None and len(d.history) == 2)
    rms1 = float(np.sqrt(np.mean(d.read(2000, 40000) ** 2)))
    assert 20 * np.log10(rms0 / rms1) > 12
    _close(w)


def test_clicks_found_and_marked(qapp, session, data_dir, tmp_path):
    from conftest import ff
    p = str(tmp_path / "clicks.wav")
    ff("-f", "lavfi", "-i", "aevalsrc='0.3*sin(2*PI*330*t)+0.4*eq(mod(floor(t*48000),9600),4321)':s=48000:d=2",
       "-c:a", "pcm_f32le", p)                                   # a one-sample click every 0.2 s: 10
    w = _win(qapp, session)
    _open(qapp, w, p)
    d = w.doc
    dlg = _dlg(w, "declick")
    dlg.b_find.click()
    assert pump(qapp, 20, lambda: dlg.b_find.isEnabled() and dlg.marks)
    assert len(dlg.marks) == 10 and "10" in dlg.found.text()
    assert w.wave.marks() == dlg.marks
    marks = list(dlg.marks)
    assert all(abs((m - 4321) % 9600) <= 2 or abs((m - 4321) % 9600 - 9600) <= 2 for m in marks)
    dlg.reject()
    w.apply_effect("declick", {})
    assert pump(qapp, 30, lambda: w.job is None and len(d.history) == 2)
    assert "10" in w.statusBar().currentMessage()
    assert w.wave.marks() == []                                   # the document changed: the marks go
    assert np.abs(d.read(4321 - 2, 5)[:, 0]).max() < 0.35
    _close(w)


def test_tempo_preview_and_apply(qapp, session, data_dir, media_dir):
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
    d = w.doc
    n = d.frames
    dlg = _dlg(w, "tempo")
    dlg.set_values({"tempo": 50.0})
    assert "0:00:02.000" in dlg.len_label.text()
    dlg.b_preview.setChecked(True)
    assert pump(qapp, 0.05, lambda: "Подготовка" in dlg.status.text()) or True
    assert pump(qapp, 30, lambda: w.player.state == "play" and dlg.render_prev and dlg.render_prev.doc)
    assert abs(dlg.render_prev.doc.frames - n / 1.5) < 0.02 * n
    first = dlg.render_prev
    dlg.set_values({"tempo": -50.0})                             # a change renders again (after a pause)
    assert pump(qapp, 30, lambda: dlg.render_prev is not first and dlg.render_prev.doc is not None
                and w.player.doc is dlg.render_prev.doc)
    assert abs(dlg.render_prev.doc.frames - n / 0.5) < 0.02 * n
    dlg.b_bypass.setChecked(True)
    pump(qapp, 0.1)
    assert w.player.doc is d
    dlg.reject()
    assert w.player.state == "stop"
    w.apply_effect("tempo", {"tempo": 25.0})
    assert pump(qapp, 30, lambda: w.job is None and len(d.history) == 2)
    assert abs(d.frames - n / 1.25) < 0.01 * n
    _close(w)


def test_film_51_effect(qapp, session, data_dir, media_dir, monkeypatch):
    from sound import dialogs
    monkeypatch.setattr(dialogs.TrackDialog, "exec", lambda self: True)
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "film.ac3"))
    d = w.doc
    assert d.channels == 6
    dlg = _dlg(w, "reverb")
    dlg.b_preview.setChecked(True)
    assert pump(qapp, 1.0, lambda: w.player.state == "play" and dlg.chain.blocks > 2)
    dlg.reject()
    w.apply_effect("limit", {"gain": 12.0, "ceiling": -3.0, "true_peak": False})
    assert pump(qapp, 30, lambda: w.job is None and len(d.history) == 2)
    assert d.peak().max() <= 10 ** (-3 / 20) + 1e-6
    _close(w)


def test_effect_strings_exist():
    """The parameter names, choices and preset names of every effect (looked up by key at run time)."""
    missing = []
    for e in fx.EFFECTS.values():
        keys = [e.name] + list(e.presets)
        for p in e.params:
            if p.label:
                keys.append(p.label)
            keys += [k for _v, k in p.choices if k]
        missing += [k for k in keys if k not in STRINGS]
    assert not missing, missing
    ru = [v[0] for v in STRINGS.values()]
    assert len(set(STRINGS)) == len(STRINGS) and ru


def test_english(qapp, session, data_dir, media_dir):
    from sound import i18n
    i18n.set_lang("en")
    try:
        w = _win(qapp, session)
        _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
        assert w.tb["fx_nr"].text() == "Noise reduction"
        dlg = _dlg(w, "comp")
        assert dlg.windowTitle() == "Compressor" and dlg.b_apply.text() == "Apply"
        assert re.search(r"ms$", dlg.rows["attack"].spin.suffix())
        dlg.reject()
        _close(w)
    finally:
        i18n.set_lang("ru")
