"""Spectral editing in the window (2.0, block 2): Restoration → Spectral editing opens its window and turns the
spectrogram on; a rectangle, a brush stroke and a lasso drawn with the mouse on a lane's spectrogram become the
selection of that channel (Ctrl: of all), Shift adds, Alt takes away, a click clears; it is drawn over the
spectrogram; listening to the selection / the result / the original plays a piece rendered ahead; Apply is one undo
step that takes the tone away in that channel only; closing brings the time selection back."""
import numpy as np
from conftest import ff, pump
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication
from test_fx_ui import _close, _open, _win

from sound.waveview import GUTTER_W


def _mouse(widget, kind, x, y, mods=Qt.NoModifier):
    ev = QMouseEvent(kind, QPointF(x, y), widget.mapToGlobal(QPointF(x, y)), Qt.LeftButton,
                     Qt.LeftButton if kind != QEvent.MouseButtonRelease else Qt.NoButton, mods)
    QApplication.sendEvent(widget, ev)


def _stroke(widget, pts, mods=Qt.NoModifier):
    _mouse(widget, QEvent.MouseButtonPress, *pts[0], mods)
    for p in pts[1:]:
        _mouse(widget, QEvent.MouseMove, *p, mods)
    _mouse(widget, QEvent.MouseButtonRelease, *pts[-1], mods)


def _file(tmp_path):
    p = str(tmp_path / "tone.wav")
    ff("-f", "lavfi", "-i", "aevalsrc='0.2*sin(2*PI*300*t)+0.2*sin(2*PI*3000*t)*between(t,1,2)|"
       "0.2*sin(2*PI*300*t)+0.2*sin(2*PI*3000*t)*between(t,1,2)':s=48000:d=3", "-c:a", "pcm_f32le", p)
    return p


def _amp(y, f):
    t = np.arange(len(y)) / 48000
    m = np.stack((np.sin(2 * np.pi * f * t), np.cos(2 * np.pi * f * t)), axis=1)
    c, *_ = np.linalg.lstsq(m, y, rcond=None)
    return float(np.hypot(*c))


def _geom(w, c, hz):
    """(y of hz, top, height) in channel c's spectrogram part."""
    v = w.wave
    for y in range(int(v.height())):
        part = v.spec_part_at(y)
        if part is not None and part[0] == c:
            _c, top, h = part
            return v.y_of_hz(hz, top, h), top, h
    raise AssertionError("no spectrogram part")


def test_draw_listen_apply(qapp, session, data_dir, tmp_path):
    w = _win(qapp, session, view_mode="wave")
    _open(qapp, w, _file(tmp_path))
    d = w.doc
    assert w.a_spectral.isEnabled() and w.a_spectral.shortcut().toString() == "Ctrl+Shift+R"
    w.a_spectral.trigger()
    panel = w.se_panel
    assert panel.isVisible() and w.wave.mode == "spec" and w.wave.tool == "rect"
    assert not panel.b_apply.isEnabled() and "Ничего" in panel.info.text()
    v = w.wave
    x1, x2 = v.x_of(int(0.9 * 48000)), v.x_of(int(2.1 * 48000))
    y_hi, _top, _h = _geom(w, 0, 4000.0)
    y_lo, _top, _h = _geom(w, 0, 2200.0)
    _stroke(v, [(x1, y_hi), ((x1 + x2) / 2, (y_hi + y_lo) / 2), (x2, y_lo)])
    sel = d.spec_sel
    assert sel is not None and sel.all_channels() == (0,) and len(sel.shapes) == 1
    lo, hi = sel.freq_span()
    assert 1900 < lo < 2500 and 3500 < hi < 4600
    assert panel.b_apply.isEnabled() and "L" in panel.info.text()
    v.repaint()
    # a click without dragging clears it; Ctrl draws on every channel; Shift adds a brush stroke
    _stroke(v, [(x1 + 5, y_hi), (x1 + 5, y_hi)])
    assert d.spec_sel is None and not panel.b_apply.isEnabled()
    _stroke(v, [(x1, y_hi), (x2, y_lo)], Qt.ControlModifier)
    assert d.spec_sel.all_channels() == (0, 1)
    panel.set_tool("brush")
    y_mid, _t, _h = _geom(w, 1, 3000.0)
    _stroke(v, [(x1, y_mid), ((x1 + x2) / 2, y_mid), (x2, y_mid)], Qt.ShiftModifier)
    assert len(d.spec_sel.shapes) == 2 and d.spec_sel.shapes[1].kind == "brush"
    assert d.spec_sel.shapes[1].chs == (1,)
    # Alt takes a lasso away
    panel.set_tool("lasso")
    xm = (x1 + x2) / 2
    _stroke(v, [(xm - 10, y_hi), (xm + 10, y_hi), (xm + 10, y_lo), (xm - 10, y_lo)], Qt.AltModifier)
    assert d.spec_sel.shapes[-1].op == "sub" and d.spec_sel.shapes[-1].kind == "lasso"
    v.repaint()
    # back to one rectangle on the left channel
    panel.set_tool("rect")
    _stroke(v, [(x1, y_hi), (x2, y_lo)])
    assert len(d.spec_sel.shapes) == 1 and d.spec_sel.all_channels() == (0,)
    # listening
    panel.listen_buttons["selection"].setChecked(True)
    assert pump(qapp, 20, lambda: panel._piece is not None and w.player.state == "play")
    assert w.player.doc is panel._piece
    panel.listen_buttons["original"].setChecked(True)
    assert not panel.listen_buttons["selection"].isChecked() and w.player.doc is d
    panel.listen_buttons["result"].setChecked(True)
    assert pump(qapp, 20, lambda: panel._piece is not None and w.player.doc is panel._piece)
    panel.listen_buttons["result"].setChecked(False)
    assert w.player.state == "stop" and panel._piece is None
    # apply: one undo step, the tone gone on the left only
    panel.action.setCurrentIndex(panel.action.findData("erase"))
    n = len(d.history)
    panel.apply()
    assert pump(qapp, 30, lambda: w.job is None and len(d.history) == n + 1)
    x = d.read(int(1.2 * 48000), int(0.6 * 48000)).astype(np.float64)
    assert _amp(x[:, 0], 3000) < 1e-3 and _amp(x[:, 1], 3000) > 0.19 and _amp(x[:, 0], 300) > 0.19
    assert "Спектральная правка" in w.a_undo.text()
    w.undo()
    assert _amp(d.read(int(1.2 * 48000), int(0.6 * 48000))[:, 0].astype(np.float64), 3000) > 0.19
    panel.close()
    assert w.wave.tool == "time" and w.se_panel is None
    _close(w)


def test_brush_size_wheel_and_both_mode(qapp, session, data_dir, tmp_path):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QWheelEvent
    w = _win(qapp, session, view_mode="both")
    _open(qapp, w, _file(tmp_path))
    w.a_spectral.trigger()
    panel = w.se_panel
    assert w.wave.mode == "both"                                      # the spectrogram already shown: kept
    panel.set_tool("brush")
    size = w.wave.brush_px
    y, _t, _h = _geom(w, 0, 1000.0)
    ev = QWheelEvent(QPointF(GUTTER_W + 100, y), QPointF(GUTTER_W + 100, y), QPoint(0, 0), QPoint(0, 240),
                     Qt.NoButton, Qt.AltModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(w.wave, ev)
    assert w.wave.brush_px > size and panel.brush.value() == w.wave.brush_px
    # the wave part of a lane is not drawn on
    for yy in range(int(w.wave.height())):
        if w.wave.spec_part_at(yy) is None and yy > 60:
            _stroke(w.wave, [(GUTTER_W + 50, yy), (GUTTER_W + 200, yy)])
            break
    assert w.doc.spec_sel is None
    panel.close()
    _close(w)
