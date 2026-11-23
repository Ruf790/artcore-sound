"""The effect windows (0.3): the parameters as sliders with spin boxes, a graph where one helps (the EQ's response with
points to drag, the compressor's curve, the gain-reduction meter, the noise sample's spectrum), presets (built-in
and the user's own, kept in the settings), and Preview / Bypass / Apply / Cancel. Preview loops the selection (or the
whole file) through the effect in the player's reader thread — the sliders change the sound as it plays; an effect
done by ffmpeg (tempo, pitch) first renders a piece ahead and shows that it is preparing."""
from __future__ import annotations

import math
import os
import threading

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGridLayout,
                               QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton, QSizePolicy, QSlider,
                               QSpinBox, QVBoxLayout, QWidget)

from . import APP_TITLE, config, fx, style
from .doc import fmt_time
from .fx import eq as eqm
from .i18n import t

SLIDER = 1000
F_LO, F_HI = 20.0, 20000.0


def unit_text(u: str) -> str:
    return {"ms": t("un_ms"), "s": t("un_s"), "Hz": t("un_Hz"), "st": t("un_st"), "ct": t("un_ct")}.get(u, u)


def param_label(p) -> str:
    if p.label:
        return t(p.label)
    return p.group or t("fx_" + p.key)


def _font(px: int, bold: bool = False) -> QFont:
    f = QFont()
    f.setPixelSize(px)
    f.setBold(bold)
    return f


# ── one parameter ─────────────────────────────────────────────
class ParamRow(QWidget):
    """A slider and a spin box (float / int), a check box (bool) or a list (choice). changed(key, value)."""
    changed = Signal(str, object)

    def __init__(self, p, value, parent=None, vertical: bool = False):
        super().__init__(parent)
        self.p = p
        self._lock = False
        h = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        self.slider = self.spin = self.check = self.combo = None
        if p.kind == "bool":
            self.check = QCheckBox(param_label(p))
            self.check.toggled.connect(lambda v: self._emit(bool(v)))
            h.addWidget(self.check)
        elif p.kind == "choice":
            self.combo = QComboBox()
            for v, key in p.choices:
                self.combo.addItem(t(key) if key else str(v), v)
            self.combo.currentIndexChanged.connect(lambda _i: self._emit(self.combo.currentData()))
            h.addWidget(self.combo)
            h.addStretch(1)
        else:
            self.slider = QSlider(Qt.Vertical if vertical else Qt.Horizontal)
            self.slider.setRange(0, SLIDER)
            self.slider.setMinimumWidth(30 if vertical else 160)
            if vertical:
                self.slider.setMinimumHeight(130)
            self.slider.valueChanged.connect(self._from_slider)
            if p.kind == "int":
                self.spin = QSpinBox()
                self.spin.setRange(int(p.lo), int(p.hi))
            else:
                self.spin = QDoubleSpinBox()
                self.spin.setRange(p.lo, p.hi)
                self.spin.setDecimals(p.decimals)
                step = p.step or (10 ** -p.decimals if p.decimals else 1.0)
                if p.log:
                    step = max(step, 10 ** -p.decimals)
                self.spin.setSingleStep(step if p.decimals else max(1.0, step))
            if p.unit:
                self.spin.setSuffix(" " + unit_text(p.unit))
            self.spin.setKeyboardTracking(False)
            self.spin.setFixedWidth(64 if vertical else 112)
            if vertical:
                self.spin.setButtonSymbols(QDoubleSpinBox.NoButtons)
                self.spin.setAlignment(Qt.AlignCenter)
            self.spin.valueChanged.connect(self._from_spin)
            if vertical:
                h.addWidget(self.slider, 1, Qt.AlignHCenter)
                h.addWidget(self.spin)
            else:
                h.addWidget(self.slider, 1)
                h.addWidget(self.spin)
        self.set_value(value)

    # slider ⇄ value
    def _pos(self, v: float) -> int:
        p = self.p
        if p.hi <= p.lo:
            return 0
        if p.log and p.lo > 0:
            f = math.log(max(v, p.lo) / p.lo) / math.log(p.hi / p.lo)
        else:
            f = (v - p.lo) / (p.hi - p.lo)
        return int(round(max(0.0, min(1.0, f)) * SLIDER))

    def _val(self, pos: int) -> float:
        p = self.p
        f = pos / SLIDER
        v = p.lo * (p.hi / p.lo) ** f if p.log and p.lo > 0 else p.lo + (p.hi - p.lo) * f
        return round(v, p.decimals) if p.kind != "int" else int(round(v))

    def _from_slider(self, pos: int) -> None:
        if self._lock:
            return
        v = self._val(pos)
        self._lock = True
        self.spin.setValue(v)
        self._lock = False
        self._emit(v)

    def _from_spin(self, v) -> None:
        if self._lock:
            return
        self._lock = True
        self.slider.setValue(self._pos(float(v)))
        self._lock = False
        self._emit(v)

    def _emit(self, v) -> None:
        if not self._lock:
            self.changed.emit(self.p.key, v)

    def value(self):
        if self.check is not None:
            return self.check.isChecked()
        if self.combo is not None:
            return self.combo.currentData()
        return self.spin.value()

    def set_value(self, v) -> None:
        self._lock = True
        try:
            if self.check is not None:
                self.check.setChecked(bool(v))
            elif self.combo is not None:
                self.combo.setCurrentIndex(max(0, self.combo.findData(v)))
            else:
                self.spin.setValue(v)
                self.slider.setValue(self._pos(float(v)))
        finally:
            self._lock = False

    def setEnabled(self, on: bool) -> None:                                  # noqa: N802
        super().setEnabled(on)


# ── graphs ────────────────────────────────────────────────────
class _Plot(QWidget):
    """A plot area with a log frequency axis (20 Hz … 20 kHz) and a dB axis."""
    L, R, T, B = 40, 12, 10, 22

    def __init__(self, db_lo: float, db_hi: float, parent=None):
        super().__init__(parent)
        self.db_lo, self.db_hi = db_lo, db_hi
        self.setMinimumSize(520, 220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def area(self) -> QRectF:
        return QRectF(self.L, self.T, self.width() - self.L - self.R, self.height() - self.T - self.B)

    def x_of(self, f: float) -> float:
        a = self.area()
        return a.left() + a.width() * math.log(max(f, F_LO) / F_LO) / math.log(F_HI / F_LO)

    def f_of(self, x: float) -> float:
        a = self.area()
        return F_LO * (F_HI / F_LO) ** max(0.0, min(1.0, (x - a.left()) / max(1.0, a.width())))

    def y_of(self, v: float) -> float:
        a = self.area()
        return a.top() + a.height() * (self.db_hi - v) / (self.db_hi - self.db_lo)

    def v_of(self, y: float) -> float:
        a = self.area()
        return self.db_hi - (y - a.top()) / max(1.0, a.height()) * (self.db_hi - self.db_lo)

    def paint_grid(self, p: QPainter, db_step: float) -> None:
        a = self.area()
        p.fillRect(self.rect(), style.qcolor(style.PANEL))
        p.fillRect(a, style.qcolor(style.W_BG))
        grid = style.qcolor(style.W_GRID)
        p.setFont(_font(10))
        for f in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            x = self.x_of(f)
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(x, a.top()), QPointF(x, a.bottom()))
            p.setPen(style.qcolor(style.MUTED))
            lab = f"{f // 1000}k" if f >= 1000 else str(f)
            p.drawText(QRectF(x - 20, a.bottom() + 3, 40, 16), Qt.AlignCenter, lab)
        v = math.ceil(self.db_lo / db_step) * db_step
        while v <= self.db_hi + 1e-9:
            y = self.y_of(v)
            p.setPen(QPen(style.qcolor(style.W_CENTER) if abs(v) < 1e-9 else grid, 1))
            p.drawLine(QPointF(a.left(), y), QPointF(a.right(), y))
            p.setPen(style.qcolor(style.MUTED))
            p.drawText(QRectF(0, y - 7, self.L - 5, 14), Qt.AlignRight | Qt.AlignVCenter,
                       f"{v:+.0f}".replace("-", "−") if v else "0")
            v += db_step
        p.setPen(QPen(style.qcolor(style.BORDER), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(a)


BAND_COLORS = ("#ff6b6b", "#ffa94d", "#ffd43b", "#69db7c", "#38d9a9", "#4dabf7", "#b197fc", "#f783ac",
               "#e599f7", "#74c0fc")


class EqGraph(_Plot):
    """The response of the EQ, a point per band to drag. mode "eq" (parametric) | "geq" (graphic: up and down)."""
    moved = Signal(int, float, float)          # band (1-based for "eq", 0-based for "geq"), frequency, gain
    wheeled = Signal(int, float)               # band, a factor for its Q
    toggled = Signal(int)

    def __init__(self, effect, rate: int, mode: str = "eq", parent=None):
        rng = 24.0 if mode == "eq" else 15.0
        super().__init__(-rng, rng, parent)
        self.effect, self.rate, self.mode = effect, rate, mode
        self.values = effect.defaults()
        self.drag = None
        self.hot = None
        self.setMouseTracking(True)
        self.setMinimumHeight(240)
        self._freqs = np.geomspace(F_LO, min(F_HI, rate / 2 * 0.999), 400)

    def set_values(self, values: dict) -> None:
        self.values = dict(values)
        self.update()

    def bands(self) -> list[tuple[int, float, float, bool, str]]:
        """[(index, f, gain shown, on, kind)]."""
        v = self.values
        if self.mode == "geq":
            return [(i, f, v[f"g{i}"], True, "peak") for i, f in enumerate(eqm.GEQ_FREQS)]
        out = []
        sos = eqm.sos_of(self.effect, v, self.rate)
        for i in range(1, eqm.N_BANDS + 1):
            kind, f = v[f"b{i}_type"], v[f"b{i}_freq"]
            on = v[f"b{i}_on"]
            if kind in ("hp", "lp"):
                g = float(eqm.response_db(sos[i - 1:i], [f], self.rate)[0]) if on else 0.0
            else:
                g = v[f"b{i}_gain"]
            out.append((i, f, g, on, kind))
        return out

    def response(self) -> np.ndarray:
        sos = eqm.sos_of(self.effect, self.values, self.rate)
        return eqm.response_db(sos, self._freqs, self.rate) + self.values.get("out", 0.0)

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self.paint_grid(p, 6.0 if self.mode == "eq" else 3.0)
        a = self.area()
        p.setClipRect(a)
        r = np.clip(self.response(), self.db_lo - 6, self.db_hi + 6)
        path = QPainterPath()
        for i, (f, v) in enumerate(zip(self._freqs, r)):
            pt = QPointF(self.x_of(f), self.y_of(v))
            path.moveTo(pt) if i == 0 else path.lineTo(pt)
        fill = QPainterPath(path)
        fill.lineTo(QPointF(self.x_of(self._freqs[-1]), self.y_of(0)))
        fill.lineTo(QPointF(self.x_of(self._freqs[0]), self.y_of(0)))
        fill.closeSubpath()
        acc = style.qcolor(style.ACCENT)
        c = QColor(acc)
        c.setAlpha(50)
        p.fillPath(fill, c)
        p.setPen(QPen(acc, 2))
        p.drawPath(path)
        p.setClipping(False)
        for i, f, g, on, _kind in self.bands():
            x, y = self.x_of(f), self.y_of(max(self.db_lo, min(self.db_hi, g)))
            col = QColor(BAND_COLORS[(i - (1 if self.mode == "eq" else 0)) % len(BAND_COLORS)])
            rad = 8 if self.hot == i or (self.drag and self.drag[0] == i) else 6.5
            if on:
                p.setPen(QPen(QColor("#000000" if not style.IS_DARK else "#ffffff"), 1.2))
                p.setBrush(col)
            else:
                p.setPen(QPen(style.qcolor(style.MUTED), 1.2, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(x, y), rad, rad)
            if self.mode == "eq":
                p.setPen(style.qcolor("#000000") if on else style.qcolor(style.MUTED))
                p.setFont(_font(9, True))
                p.drawText(QRectF(x - 8, y - 8, 16, 16), Qt.AlignCenter, str(i))

    def _hit(self, pos) -> int | None:
        best, bd = None, 13.0
        for i, f, g, _on, _k in self.bands():
            d = math.hypot(pos.x() - self.x_of(f), pos.y() - self.y_of(max(self.db_lo, min(self.db_hi, g))))
            if d < bd:
                best, bd = i, d
        return best

    def mousePressEvent(self, ev) -> None:
        i = self._hit(ev.position())
        if i is not None and ev.button() == Qt.LeftButton:
            self.drag = (i,)
            self.update()

    def mouseMoveEvent(self, ev) -> None:
        pos = ev.position()
        if self.drag is None:
            h = self._hit(pos)
            if h != self.hot:
                self.hot = h
                self.setCursor(Qt.PointingHandCursor if h is not None else Qt.ArrowCursor)
                self.update()
            return
        i = self.drag[0]
        g = round(max(self.db_lo, min(self.db_hi, self.v_of(pos.y()))), 1)
        if self.mode == "geq":
            g = max(-12.0, min(12.0, g))
            self.moved.emit(i, eqm.GEQ_FREQS[i], g)
        else:
            f = round(max(F_LO, min(F_HI, self.f_of(pos.x()))), 0)
            self.moved.emit(i, f, max(-24.0, min(24.0, g)))

    def mouseReleaseEvent(self, ev) -> None:
        self.drag = None
        self.update()

    def mouseDoubleClickEvent(self, ev) -> None:
        i = self._hit(ev.position())
        if i is not None and self.mode == "eq":
            self.toggled.emit(i)

    def wheelEvent(self, ev) -> None:
        i = self._hit(ev.position()) if self.hot is None else self.hot
        if i is not None and self.mode == "eq":
            self.wheeled.emit(i, 1.12 if ev.angleDelta().y() > 0 else 1 / 1.12)
            ev.accept()


class GrMeter(QWidget):
    """The gain reduction: a bar from the top, 0 … 24 dB, the figure under it; the peak falls back slowly."""

    def __init__(self, parent=None, top: float = 24.0):
        super().__init__(parent)
        self.top = top
        self.v = 0.0
        self.hold = 0.0
        self.setFixedWidth(78)
        self.setMinimumHeight(160)

    def set(self, v: float) -> None:
        v = max(0.0, float(v))
        self.v = v if v > self.v else self.v * 0.85 + v * 0.15
        self.hold = max(v, self.hold - 0.3)
        self.update()

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), style.qcolor(style.PANEL))
        top, bottom = 22, self.height() - 22
        x, w = 22, 18
        p.fillRect(QRectF(x, top, w, bottom - top), style.qcolor(style.W_METER))
        h = (bottom - top) * min(1.0, self.v / self.top)
        p.fillRect(QRectF(x, top, w, h), QColor("#f08c00"))
        hy = top + (bottom - top) * min(1.0, self.hold / self.top)
        p.fillRect(QRectF(x, hy - 1, w, 2), style.qcolor(style.TEXT))
        p.setFont(_font(9))
        p.setPen(style.qcolor(style.MUTED))
        for d in (0, 6, 12, 18, 24):
            if d <= self.top:
                y = top + (bottom - top) * d / self.top
                p.drawText(QRectF(x + w + 2, y - 6, 22, 12), Qt.AlignLeft | Qt.AlignVCenter, str(d))
        p.drawText(QRectF(0, 0, self.width(), 16), Qt.AlignCenter, t("gr_meter"))
        p.setPen(style.qcolor(style.TEXT))
        p.setFont(_font(10, True))
        p.drawText(QRectF(0, bottom + 3, self.width(), 16), Qt.AlignCenter, f"−{self.v:.1f}" if self.v >= 0.05
                   else "0.0")


class CompGraph(QWidget):
    """The compressor's static curve: in → out, dB, −60 … 0 (+ the make-up)."""

    def __init__(self, effect, parent=None):
        super().__init__(parent)
        self.effect = effect
        self.values = effect.defaults()
        self.level = None                      # the input level now (a dot on the curve)
        self.setMinimumSize(230, 230)

    def set_values(self, v: dict) -> None:
        self.values = dict(v)
        self.update()

    def paintEvent(self, ev) -> None:
        from .fx.dynamics import static_gr
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), style.qcolor(style.PANEL))
        s = min(self.width() - 40, self.height() - 34)
        a = QRectF(32, 8, s, s)
        p.fillRect(a, style.qcolor(style.W_BG))
        lo = -60.0

        def pt(i, o):
            return QPointF(a.left() + a.width() * (i - lo) / -lo, a.top() + a.height() * (o / lo))
        p.setFont(_font(9))
        for d in range(-60, 1, 12):
            p.setPen(QPen(style.qcolor(style.W_GRID), 1))
            p.drawLine(pt(d, lo), pt(d, 0))
            p.drawLine(pt(lo, d), pt(0, d))
            p.setPen(style.qcolor(style.MUTED))
            p.drawText(QRectF(0, pt(lo, d).y() - 6, 28, 12), Qt.AlignRight | Qt.AlignVCenter, str(d).replace("-", "−"))
            p.drawText(QRectF(pt(d, lo).x() - 14, a.bottom() + 2, 28, 12), Qt.AlignCenter, str(d).replace("-", "−"))
        p.setPen(QPen(style.qcolor(style.W_CENTER), 1, Qt.DashLine))
        p.drawLine(pt(lo, lo), pt(0, 0))
        v = self.effect.clean(self.values)
        xs = np.linspace(lo, 0, 241)
        ys = xs - static_gr(xs, v["threshold"], v["ratio"], v["knee"]) + v["makeup"]
        path = QPainterPath()
        p.setClipRect(a)
        for i, (x, y) in enumerate(zip(xs, ys)):
            path.moveTo(pt(x, y)) if i == 0 else path.lineTo(pt(x, y))
        p.setPen(QPen(style.qcolor(style.ACCENT), 2))
        p.drawPath(path)
        p.setPen(QPen(QColor("#f08c00"), 1, Qt.DotLine))
        p.drawLine(pt(v["threshold"], lo), pt(v["threshold"], 0))
        p.setClipping(False)
        p.setPen(QPen(style.qcolor(style.BORDER), 1))
        p.drawRect(a)


class NoiseGraph(_Plot):
    """The noise sample's spectrum (dB per bin, the channels' mean) and the gate's threshold over it."""

    def __init__(self, parent=None):
        super().__init__(-130.0, -20.0, parent)
        self.setMinimumHeight(170)
        self.prof = None
        self.sens = 6.0

    def set(self, prof, sens: float) -> None:
        self.prof, self.sens = prof, sens
        if prof is not None:
            lv = self._levels()
            hi = float(np.percentile(lv, 99.5)) if len(lv) else -20.0
            self.db_hi = min(0.0, math.ceil((hi + self.sens + 12) / 10) * 10)
            self.db_lo = self.db_hi - 100.0
        self.update()

    def _levels(self):
        n = self.prof["n"]
        win = np.sqrt(np.hanning(n))
        ref = (win.sum() / 2) ** 2                      # a full-scale sine's bin power
        pw = self.prof["power"].mean(axis=1)
        return 10 * np.log10(np.maximum(pw, 1e-30) / ref)

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self.paint_grid(p, 20.0)
        a = self.area()
        if self.prof is None:
            p.setPen(style.qcolor(style.MUTED))
            p.drawText(a, Qt.AlignCenter | Qt.TextWordWrap, t("nr_no_profile"))
            return
        lv = self._levels()
        n = self.prof["n"]
        freqs = np.arange(len(lv)) * self.prof["rate"] / n
        keep = freqs >= F_LO
        p.setClipRect(a)
        for off, col, w in ((0.0, style.qcolor(style.MUTED), 1.5), (self.sens, QColor("#f08c00"), 1.5)):
            path = QPainterPath()
            first = True
            for f, v in zip(freqs[keep], lv[keep]):
                q = QPointF(self.x_of(f), self.y_of(v + off))
                path.moveTo(q) if first else path.lineTo(q)
                first = False
            p.setPen(QPen(col, w))
            p.drawPath(path)


# ── the window of an effect ───────────────────────────────────
class EffectDialog(QDialog):
    """One effect over [a, b) of the window's document (the selection, or the whole file)."""
    scanned = Signal(object, int)                      # clicks found (frames), run

    def __init__(self, win, key: str, d, a: int, b: int, whole: bool, batch: dict | None = None):
        """batch (1.0): the dialog edits a step of a batch — {"values": …, "file": the name of the file previewed
        (d is its first seconds; an empty d: no preview)}; Apply becomes OK and nothing is applied."""
        super().__init__(win)
        self.win, self.key, self.d, self.a, self.b, self.whole = win, key, d, a, b, whole
        self.batch = batch
        self.eff = fx.get(key)
        cfg = win.cfg
        if batch is not None:
            last = dict(batch.get("values") or {})
            self.profile_path = str(last.get("profile") or "")
            if key == "nr":
                import copy
                self.eff = copy.copy(self.eff)
                self.eff.profile = None
                self._load_profile(self.profile_path)
        else:
            last = (cfg.get("fx_last") or {}).get(key) or {}
        self.values = self.eff.clean({**self.eff.defaults(), **last})
        self.rows: dict = {}
        self.chain = None                               # the PreviewChain while previewing in real time
        self.render_prev = None                         # fx.PreviewRender of a non-real-time effect
        self._render_docs: list = []
        self.graph = self.comp_graph = self.gr = self.noise_graph = None
        self.marks: list = []
        self._scan_run = 0
        self.setWindowTitle(t(self.eff.name))
        self.setMinimumWidth({"eq": 980, "geq": 760, "comp": 940, "limit": 800, "nr": 760}.get(key, 700))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(8)
        head = QLabel(t(self.eff.name))
        head.setStyleSheet("font-size: 13pt; font-weight: 700; padding-bottom: 2px;")
        lay.addWidget(head)
        if batch is not None:
            what = t("bt_fx_what", batch.get("file")) if d.frames else t("bt_fx_no_preview")
        else:
            what = t("p_whole") if whole else t("fx_sel", fmt_time(a, d.rate), fmt_time(b, d.rate),
                                                 fmt_time(b - a, d.rate))
        self.what = QLabel(what)
        self.what.setObjectName("hint")
        self.what.setWordWrap(True)
        lay.addWidget(self.what)
        lay.addLayout(self._presets_row())
        body = QWidget()
        self._build_body(body)
        lay.addWidget(body, 1)
        self.keep_tail = None
        if self.eff.has_tail:
            self.keep_tail = QCheckBox(t("fx_keep_tail"))
            self.keep_tail.setChecked(bool(cfg.get("fx_keep_tail", True)) if batch is None else
                                      bool(batch.get("values", {}).get("keep_tail", True)))
            lay.addWidget(self.keep_tail)
        self.status = QLabel("")
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        row = QHBoxLayout()
        self.b_preview = QPushButton(style.icon("fa6s.play"), t("fx_preview"))
        self.b_preview.setCheckable(True)
        self.b_preview.setToolTip(t("fx_preview_tip"))
        self.b_preview.toggled.connect(self._preview_toggled)
        self.b_bypass = QPushButton(style.icon("fa6s.power-off"), t("fx_bypass"))
        self.b_bypass.setCheckable(True)
        self.b_bypass.setToolTip(t("fx_bypass_tip"))
        self.b_bypass.toggled.connect(self._bypass_toggled)
        row.addWidget(self.b_preview)
        row.addWidget(self.b_bypass)
        row.addStretch(1)
        self.b_apply = QPushButton(t("fx_apply") if batch is None else t("ok"))
        if batch is not None and not d.frames:
            self.b_preview.setEnabled(False)
            self.b_bypass.setEnabled(False)
        self.b_apply.setObjectName("primary")
        self.b_apply.setDefault(True)
        self.b_apply.clicked.connect(self.accept)
        b_cancel = QPushButton(t("cancel"))
        b_cancel.clicked.connect(self.reject)
        row.addWidget(self.b_apply)
        row.addWidget(b_cancel)
        lay.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(400)
        self.debounce.timeout.connect(self._rerender)
        self.scanned.connect(self._scanned)
        self._sync()

    # ── building ──────────────────────────────────────────────
    def _row(self, key: str, vertical: bool = False) -> ParamRow:
        r = ParamRow(self.eff.by_key[key], self.values[key], self, vertical)
        r.changed.connect(self._changed)
        self.rows[key] = r
        return r

    def _form(self, keys) -> QFormLayout:
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(6)
        for k in keys:
            p = self.eff.by_key[k]
            r = self._row(k)
            if p.kind == "bool":
                form.addRow("", r)
            else:
                form.addRow(param_label(p), r)
        return form

    def _build_body(self, body: QWidget) -> None:
        k = self.key
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        if k == "eq":
            self.graph = EqGraph(self.eff, self.d.rate, "eq")
            self.graph.moved.connect(self._eq_moved)
            self.graph.wheeled.connect(self._eq_wheel)
            self.graph.toggled.connect(lambda i: self._set(f"b{i}_on", not self.values[f"b{i}_on"]))
            v.addWidget(self.graph, 1)
            v.addWidget(self._hint(t("eq_hint")))
            grid = QGridLayout()
            grid.setHorizontalSpacing(6)
            grid.setVerticalSpacing(4)
            names = ("eq_on", "eq_type", "eq_freq", "eq_gain", "eq_q")
            for r, name in enumerate(names):
                lab = QLabel(t(name))
                lab.setObjectName("hint")
                grid.addWidget(lab, r + 1, 0)
            for i in range(1, eqm.N_BANDS + 1):
                cap = QLabel(t("eq_band", i))
                cap.setStyleSheet(f"color: {BAND_COLORS[(i - 1) % len(BAND_COLORS)]}; font-weight: 700;")
                grid.addWidget(cap, 0, i, Qt.AlignCenter)
                for r, suffix in enumerate(("on", "type", "freq", "gain", "q")):
                    key = f"b{i}_{suffix}"
                    p = self.eff.by_key[key]
                    w = self._band_widget(p)
                    grid.addWidget(w, r + 1, i)
            v.addLayout(grid)
            v.addLayout(self._form(["out"]))
        elif k == "geq":
            self.graph = EqGraph(self.eff, self.d.rate, "geq")
            self.graph.moved.connect(lambda i, _f, g: self._set(f"g{i}", g))
            v.addWidget(self.graph, 1)
            v.addWidget(self._hint(t("geq_hint")))
            h = QHBoxLayout()
            h.setSpacing(4)
            for i, f in enumerate(eqm.GEQ_FREQS):
                col = QVBoxLayout()
                r = self._row(f"g{i}", vertical=True)
                r.spin.setSuffix("")
                col.addWidget(r, 1)
                lab = QLabel(eqm._geq_label(f))
                lab.setAlignment(Qt.AlignCenter)
                lab.setObjectName("hint")
                col.addWidget(lab)
                h.addLayout(col)
            v.addLayout(h)
            v.addLayout(self._form(["out"]))
        elif k in ("comp", "limit"):
            h = QHBoxLayout()
            keys = [p.key for p in self.eff.params]
            h.addLayout(self._form(keys), 1)
            if k == "comp":
                self.comp_graph = CompGraph(self.eff)
                h.addWidget(self.comp_graph)
            self.gr = GrMeter(top=24.0)
            h.addWidget(self.gr)
            v.addLayout(h)
        elif k == "nr":
            self.noise_graph = NoiseGraph()
            v.addWidget(self.noise_graph, 1)
            self.prof_label = self._hint("")
            if self.batch is not None:
                row = QHBoxLayout()
                row.addWidget(self.prof_label, 1)
                b = QPushButton(style.icon("fa6s.folder-open"), t("bt_profile_pick") + "…")
                b.clicked.connect(self._pick_profile)
                row.addWidget(b)
                v.addLayout(row)
            else:
                v.addWidget(self.prof_label)
            v.addLayout(self._form([p.key for p in self.eff.params]))
        elif k == "declick":
            v.addLayout(self._form([p.key for p in self.eff.params]))
            row = QHBoxLayout()
            self.b_find = QPushButton(style.icon("fa6s.magnifying-glass"), t("dc_find"))
            self.b_find.clicked.connect(self._find_clicks)
            row.addWidget(self.b_find)
            self.found = QLabel("")
            row.addWidget(self.found, 1)
            v.addLayout(row)
            if self.batch is not None:
                self.b_find.hide()
        else:
            v.addLayout(self._form([p.key for p in self.eff.params]))
            if k == "reverb":
                v.addWidget(self._hint(t("rv_note")))
            if k == "tempo":
                from .fx import tempo as tm
                self.len_label = QLabel("")
                v.addWidget(self.len_label)
                v.addWidget(self._hint(t("tp_note") + ("" if tm.has_rubberband() else " " + t("tp_no_rb"))))
        v.addStretch(0)

    def _band_widget(self, p) -> QWidget:
        """A compact field of the EQ's band table (no slider: the graph is the slider)."""
        if p.kind == "bool":
            w = QCheckBox()
            w.setChecked(bool(self.values[p.key]))
            w.toggled.connect(lambda on, k=p.key: self._changed(k, bool(on)))
        elif p.kind == "choice":
            w = QComboBox()
            for val, key in p.choices:
                w.addItem(t(key), val)
            w.setCurrentIndex(max(0, w.findData(self.values[p.key])))
            w.currentIndexChanged.connect(lambda _i, k=p.key, w=w: self._changed(k, w.currentData()))
        else:
            w = QDoubleSpinBox()
            w.setRange(p.lo, p.hi)
            w.setDecimals(p.decimals)
            w.setSingleStep({"eq_freq": 10.0, "eq_gain": 0.5, "eq_q": 0.1}.get(p.label, 1.0))
            if p.unit:
                w.setSuffix(" " + unit_text(p.unit))
            w.setKeyboardTracking(False)
            w.setValue(self.values[p.key])
            w.valueChanged.connect(lambda val, k=p.key: self._changed(k, val))
        w.setProperty("fxKey", p.key)
        self.rows[p.key] = w
        return w

    def _hint(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("hint")
        lab.setWordWrap(True)
        return lab

    def _presets_row(self) -> QHBoxLayout:
        h = QHBoxLayout()
        h.addWidget(QLabel(t("fx_preset")))
        self.presets = QComboBox()
        self.presets.setMinimumWidth(240)
        h.addWidget(self.presets, 1)
        self.b_psave = QPushButton(t("fx_preset_save"))
        self.b_psave.clicked.connect(self._save_preset)
        self.b_pdel = QPushButton(t("fx_preset_del"))
        self.b_pdel.clicked.connect(self._delete_preset)
        h.addWidget(self.b_psave)
        h.addWidget(self.b_pdel)
        self._fill_presets()
        self.presets.activated.connect(self._preset_chosen)
        return h

    def _user_presets(self) -> dict:
        return (self.win.cfg.get("fx_presets") or {}).get(self.key) or {}

    def _fill_presets(self, select: str | None = None) -> None:
        c = self.presets
        c.blockSignals(True)
        c.clear()
        c.addItem(t("fx_preset_default"), "default:")
        for name in self.eff.presets:
            c.addItem(t(name), "builtin:" + name)
        mine = self._user_presets()
        if mine:
            c.insertSeparator(c.count())
            for name in sorted(mine, key=str.lower):
                c.addItem("★ " + name, "user:" + name)
        if select is not None:
            c.setCurrentIndex(max(0, c.findData("user:" + select)))
        else:
            c.setCurrentIndex(-1)
            c.setPlaceholderText("—")
        c.blockSignals(False)
        self._preset_buttons()

    def _preset_buttons(self) -> None:
        data = self.presets.currentData()
        self.b_pdel.setEnabled(bool(data) and data.startswith("user:"))

    def preset_values(self, data: str) -> dict:
        """data: "builtin:<key>" | "user:<name>" | "default:"."""
        kind, _sep, name = data.partition(":")
        if kind == "builtin":
            base = self.eff.presets.get(name, {})
        elif kind == "user":
            base = self._user_presets().get(name, {})
        else:
            base = {}
        return self.eff.clean({**self.eff.defaults(), **base})

    def _preset_chosen(self, _i: int) -> None:
        data = self.presets.currentData()
        if data:
            self.set_values(self.preset_values(data))
        self._preset_buttons()

    def _save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, t("fx_preset_save"), t("fx_preset_name"))
        name = (name or "").strip()
        if ok and name:
            self.save_preset(name)

    def save_preset(self, name: str) -> None:
        cfg = self.win.cfg
        cfg.setdefault("fx_presets", {}).setdefault(self.key, {})[name] = dict(self.values)
        config.save(cfg)
        self._fill_presets(name)

    def _delete_preset(self) -> None:
        data = self.presets.currentData()
        if data and data.startswith("user:"):
            cfg = self.win.cfg
            cfg.get("fx_presets", {}).get(self.key, {}).pop(data.partition(":")[2], None)
            config.save(cfg)
            self._fill_presets()

    # ── values ────────────────────────────────────────────────
    def set_values(self, values: dict) -> None:
        self.values = self.eff.clean(values)
        for k, w in self.rows.items():
            self._show(k, w, self.values[k])
        self._sync()
        self._push()

    @staticmethod
    def _show(k, w, v) -> None:
        if isinstance(w, ParamRow):
            w.set_value(v)
            return
        w.blockSignals(True)
        if isinstance(w, QCheckBox):
            w.setChecked(bool(v))
        elif isinstance(w, QComboBox):
            w.setCurrentIndex(max(0, w.findData(v)))
        else:
            w.setValue(v)
        w.blockSignals(False)

    def _set(self, key: str, v) -> None:
        """A value from a graph: into the field too."""
        self.values[key] = self.eff.by_key[key].clean(v)
        w = self.rows.get(key)
        if w is not None:
            self._show(key, w, self.values[key])
        self._sync()
        self._push()

    def _changed(self, key: str, v) -> None:
        self.values[key] = self.eff.by_key[key].clean(v)
        self._sync()
        self._push()

    def _eq_moved(self, i: int, f: float, g: float) -> None:
        kind = self.values[f"b{i}_type"]
        if not self.values[f"b{i}_on"]:
            self._set(f"b{i}_on", True)
        self._set(f"b{i}_freq", f)
        if kind not in ("hp", "lp"):
            self._set(f"b{i}_gain", g)

    def _eq_wheel(self, i: int, k: float) -> None:
        self._set(f"b{i}_q", round(self.values[f"b{i}_q"] * k, 2))

    def _sync(self) -> None:
        """What depends on the values: the graphs, the fields that apply, the notes."""
        v = self.values
        if self.graph is not None:
            self.graph.set_values(v)
        if self.comp_graph is not None:
            self.comp_graph.set_values(v)
        if self.key == "delay":
            for k in ("bpm", "note"):
                self.rows[k].setEnabled(v["sync"])
            self.rows["time"].setEnabled(not v["sync"])
            self.rows["pingpong"].setEnabled(self.d.channels == 2 or self.batch is not None)
        if self.key == "eq":
            for i in range(1, eqm.N_BANDS + 1):
                on = v[f"b{i}_on"]
                for s in ("type", "freq", "gain", "q"):
                    self.rows[f"b{i}_{s}"].setEnabled(on and not (s == "gain" and v[f"b{i}_type"] in ("hp", "lp")))
        if self.key == "tempo":
            n = self.b - self.a
            self.len_label.setText(t("tp_length", fmt_time(n, self.d.rate),
                                     fmt_time(self.eff.length_after(v, n), self.d.rate)))
        if self.key == "nr":
            prof = self.eff.profile
            if self.batch is not None and prof is None:
                self.prof_label.setText(t("bt_profile_none") if not self.profile_path else
                                        t("bt_profile_bad", os.path.basename(self.profile_path)))
                self.noise_graph.set(None, v["sensitivity"])
            elif self.batch is not None:
                self.prof_label.setText(t("bt_profile", os.path.basename(self.profile_path),
                                          f"{prof.get('seconds', 0):.2f}", prof["rate"], prof["channels"]))
                self.noise_graph.set(prof, v["sensitivity"])
            elif prof is None:
                self.prof_label.setText(t("nr_no_profile"))
                self.noise_graph.set(None, v["sensitivity"])
            elif prof["rate"] != self.d.rate:
                self.prof_label.setText(t("nr_other_rate", prof["rate"], self.d.rate))
                self.noise_graph.set(None, v["sensitivity"])
            else:
                self.prof_label.setText(t("nr_profile", f"{prof.get('seconds', 0):.2f}", prof["rate"],
                                          prof["channels"]))
                self.noise_graph.set(prof, v["sensitivity"])

    def _push(self) -> None:
        if self.chain is not None:
            self.chain.set(self.values)
        if self.render_prev is not None or (self.b_preview.isChecked() and not self.eff.realtime):
            self.debounce.start()

    # ── the preview ───────────────────────────────────────────
    def _preview_toggled(self, on: bool) -> None:
        self.b_preview.setIcon(style.icon("fa6s.stop" if on else "fa6s.play"))
        self.b_preview.setText(t("stop") if on else t("fx_preview"))
        if on:
            self.start_preview()
        else:
            self.stop_preview()

    def start_preview(self) -> None:
        self.stop_preview(False)
        w = self.win
        w.stop_play()
        if self.eff.realtime:
            self.chain = fx.PreviewChain(self.eff, self.d.rate, self.d.labels, self.values)
            self.chain.set_bypass(self.b_bypass.isChecked())
            w.player.play(self.d, self.a, self.b, loop=True, fx=self.chain)
            w._update_transport()
        else:
            self._rerender()

    def _rerender(self) -> None:
        if not self.b_preview.isChecked() or self.eff.realtime:
            return
        if self.render_prev is not None:
            self.render_prev.cancel()
        self.render_prev = fx.PreviewRender(self.d, self.a, self.b, self.eff, self.values, self.win.session.folder())
        self.render_prev.start()
        self.status.setText(t("fx_preparing", 0))

    def _play_rendered(self) -> None:
        rp = self.render_prev
        w = self.win
        w.player.stop()
        if self.b_bypass.isChecked() or rp is None or rp.doc is None:
            w.player.play(self.d, self.a, rp.b if rp else self.b, loop=True)
        else:
            w.player.play(rp.doc, 0, rp.doc.frames, loop=True)
        w._update_transport()

    def stop_preview(self, uncheck: bool = True) -> None:
        self.debounce.stop()
        if self.chain is not None or self.render_prev is not None or self.win.player.state != "stop":
            self.win.stop_play()
        self.chain = None
        if self.render_prev is not None:
            self.render_prev.cancel()
            rp = self.render_prev
            if rp.done.is_set() and rp.doc is not None and all(rp.doc is not x for x in self._render_docs):
                self._render_docs.append(rp.doc)
            self.render_prev = None
        for doc in self._render_docs:
            doc.close()
        self._render_docs = []
        if uncheck and self.b_preview.isChecked():
            self.b_preview.blockSignals(True)
            self.b_preview.setChecked(False)
            self.b_preview.blockSignals(False)
            self.b_preview.setIcon(style.icon("fa6s.play"))
            self.b_preview.setText(t("fx_preview"))
        if not self.eff.realtime:
            self.status.setText("")

    def _bypass_toggled(self, on: bool) -> None:
        if self.chain is not None:
            self.chain.set_bypass(on)
        elif self.render_prev is not None and self.render_prev.doc is not None:
            self._play_rendered()

    def _tick(self) -> None:
        if self.gr is not None:
            self.gr.set(self.chain.gr_db if self.chain is not None and not self.b_bypass.isChecked() else 0.0)
        rp = self.render_prev
        if rp is not None:
            if rp.done.is_set() and rp.doc is not None and all(rp.doc is not x for x in self._render_docs):
                old = list(self._render_docs)
                self._render_docs = [rp.doc]
                self._play_rendered()
                for doc in old:
                    doc.close()
                self.status.setText(t("fx_prepared", f"{rp.doc.frames / self.d.rate:.1f}"))
            elif rp.done.is_set() and rp.error:
                self.status.setText(t("fx_failed", rp.error))
            elif not rp.done.is_set():
                self.status.setText(t("fx_preparing", int(rp.frac * 100)))
        if self.chain is not None and self.chain.error:
            self.status.setText(t("fx_failed", self.chain.error))
        if self.b_preview.isChecked() and self.chain is not None and self.win.player.state != "play":
            self.stop_preview()

    # ── clicks ────────────────────────────────────────────────
    def _find_clicks(self) -> None:
        from .media import Progress
        self._scan_run += 1
        run = self._scan_run
        vals = dict(self.values)
        d, a, b = self.d, self.a, self.b
        self.b_find.setEnabled(False)
        self.found.setText(t("dc_searching", 0))
        prog = Progress(lambda f: setattr(self, "_scan_frac", f))
        self._scan_frac = 0.0

        def work():
            try:
                marks = fx.scan_clicks(d, a, b, vals, prog)
            except Exception:                              # noqa: BLE001
                marks = []
            self.scanned.emit(marks, run)
        threading.Thread(target=work, daemon=True).start()

    def _scanned(self, marks, run) -> None:
        if run != self._scan_run:
            return
        self.marks = list(marks)
        self.b_find.setEnabled(True)
        self.found.setText(t("dc_found", len(self.marks)))
        self.win.wave.set_marks(self.d, self.marks)

    # ── the result ────────────────────────────────────────────
    def result_values(self) -> dict:
        v = dict(self.values)
        if self.batch is not None:
            if self.key == "nr":
                v["profile"] = self.profile_path
            if self.keep_tail is not None:
                v["keep_tail"] = self.keep_tail.isChecked()
        return v

    # ── the noise sample of a batch step (a file) ─────────────
    def _load_profile(self, path: str) -> None:
        from . import batch as bt
        self.eff.profile = None
        if path:
            try:
                self.eff.profile = bt.adapt_noise_profile(bt.load_noise_profile(path), self.d.rate)
            except (OSError, ValueError, KeyError):
                self.eff.profile = None

    def _pick_profile(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        from . import batch as bt
        start = self.profile_path or self.win.cfg.get("noise_dir") or ""
        path, _f = QFileDialog.getOpenFileName(self, t("bt_profile_pick"), start,
                                               t("nr_file_filter") + f" (*{bt.NOISE_EXT})")
        if not path:
            return
        self.profile_path = path
        self._load_profile(path)
        if self.eff.profile is None:
            QMessageBox.warning(self, APP_TITLE, t("bt_profile_bad", os.path.basename(path)))
        if self.chain is not None:
            self.start_preview()
        self._sync()

    def keep_tail_on(self) -> bool:
        return bool(self.keep_tail is not None and self.keep_tail.isChecked())

    def done(self, r: int) -> None:
        self.timer.stop()
        self.stop_preview()
        if r and self.batch is None:
            cfg = self.win.cfg
            cfg.setdefault("fx_last", {})[self.key] = dict(self.values)
            if self.keep_tail is not None:
                cfg["fx_keep_tail"] = self.keep_tail.isChecked()
            config.save(cfg)
        super().done(r)
