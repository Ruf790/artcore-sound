"""The analysis windows (0.4): the spectrum plot (a curve per channel or of their mix, the main peak marked, the
frequency, level and note under the mouse), its settings row, the spectrum panel at the bottom of the main window
(the selection's spectrum, or while playing the live spectrum of what is heard — the effect being previewed
included), Tools → Spectrum (a larger window; copy / save CSV) and the spectrogram's settings."""
from __future__ import annotations

import math
import threading

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QProgressBar, QSizePolicy, QToolButton,
                               QVBoxLayout, QWidget)

from . import media, style
from . import spectrogram as sg
from . import spectrum as spm
from .doc import fmt_time
from .i18n import t

CH_COLORS = ("#3ea6ff", "#ff6b6b", "#ffd43b", "#69db7c", "#b197fc", "#ffa94d", "#38d9a9", "#f783ac")
CH_COLORS_LIGHT = ("#1662c4", "#d6336c", "#c48800", "#2b8a3e", "#7048e8", "#e8590c", "#0c8599", "#a61e4d")
SUM_COLOR = ("#e8e8e8", "#202020")
PANEL_DELAY = 300            # ms after the selection stops changing


def _font(px: int, bold: bool = False) -> QFont:
    f = QFont()
    f.setPixelSize(px)
    f.setBold(bold)
    return f


def fmt_hz(f: float) -> str:
    return f"{f:,.1f}".replace(",", " ") + " Hz"


def fmt_level(v: float) -> str:
    return "−∞" if v <= spm.FLOOR_DB + 1 else f"{v:.1f}".replace("-", "−")


# ── the plot ──────────────────────────────────────────────────
class SpectrumPlot(QWidget):
    L, R, T, B = 46, 12, 22, 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self.st = spm.Settings()
        self.res: spm.Result | None = None
        self.levels = None                # (curves, bins) dB as drawn
        self.live = False
        self.hover_x: float | None = None
        self.message = ""
        self.setMouseTracking(True)
        self.setMinimumSize(360, 140)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def sizeHint(self) -> QSize:
        return QSize(800, 200)

    def set_settings(self, st: spm.Settings) -> None:
        self.st = st
        if self.res is not None:
            self.levels = self.res.levels(st.smooth)
        self.update()

    def set_result(self, res: spm.Result | None, live: bool = False, message: str = "") -> None:
        self.res, self.live, self.message = res, live, message
        self.levels = res.levels(self.st.smooth) if res is not None else None
        self.update()

    # geometry
    def area(self) -> QRectF:
        return QRectF(self.L, self.T, max(1, self.width() - self.L - self.R), max(1, self.height() - self.T - self.B))

    def f_range(self) -> tuple[float, float]:
        top = (self.res.rate if self.res is not None else 48000) / 2
        return (20.0, top) if self.st.log_freq else (0.0, top)

    def x_of(self, f):
        a = self.area()
        lo, hi = self.f_range()
        f = np.asarray(f, np.float64)
        if self.st.log_freq:
            u = np.log(np.maximum(f, lo) / lo) / math.log(hi / lo)
        else:
            u = f / hi
        return a.left() + u * a.width()

    def f_of(self, x: float) -> float:
        a = self.area()
        lo, hi = self.f_range()
        u = max(0.0, min(1.0, (x - a.left()) / a.width()))
        return lo * (hi / lo) ** u if self.st.log_freq else u * hi

    def y_of(self, v):
        a = self.area()
        st = self.st
        return a.top() + (st.db_hi - np.asarray(v, np.float64)) / (st.db_hi - st.db_lo) * a.height()

    def colors(self) -> list[QColor]:
        if self.res is not None and self.res.labels == ["Σ"]:
            return [QColor(SUM_COLOR[0 if style.IS_DARK else 1])]
        pal = CH_COLORS if style.IS_DARK else CH_COLORS_LIGHT
        n = self.levels.shape[0] if self.levels is not None else 0
        return [QColor(pal[i % len(pal)]) for i in range(n)]

    # painting
    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), style.qcolor(style.PANEL))
        a = self.area()
        p.fillRect(a, style.qcolor(style.W_BG))
        self._grid(p, a)
        if self.levels is None:
            if self.message:
                p.setPen(style.qcolor(style.MUTED))
                p.setFont(_font(12))
                p.drawText(a, Qt.AlignCenter | Qt.TextWordWrap, self.message)
            return
        res = self.res
        p.save()
        p.setClipRect(a)
        p.setRenderHint(QPainter.Antialiasing, True)
        freqs = res.freqs
        ok = freqs > (self.f_range()[0] * 0.999 if self.st.log_freq else -1)
        xs = self.x_of(freqs[ok])
        for c, col in enumerate(self.colors()):
            ys = self.y_of(np.clip(self.levels[c, ok], self.st.db_lo - 10, self.st.db_hi + 10))
            px, py = _reduce_points(xs, ys)
            p.setPen(QPen(col, 1.3))
            p.drawPolyline(QPolygonF([QPointF(float(x), float(y)) for x, y in zip(px, py)]))
        p.restore()
        self._peak(p, a)
        self._legend(p, a)
        self._hover(p, a)

    def _grid(self, p: QPainter, a: QRectF) -> None:
        grid = style.qcolor(style.W_GRID)
        p.setFont(_font(10))
        lo, hi = self.f_range()
        if self.st.log_freq:
            ticks = [f for f in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 40000, 80000) if lo <= f <= hi]
        else:
            step = next((s for s in (500, 1000, 2000, 5000, 10000, 20000) if a.width() * s / hi >= 60), 20000)
            ticks = list(np.arange(0, hi + 1, step))
        for f in ticks:
            x = float(self.x_of(f))
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(x, a.top()), QPointF(x, a.bottom()))
            p.setPen(style.qcolor(style.MUTED))
            p.drawText(QRectF(x - 24, a.bottom() + 3, 48, 15), Qt.AlignCenter, sg.fmt_freq(f))
        st = self.st
        span = st.db_hi - st.db_lo
        step = 10 if a.height() / (span / 10) >= 18 else 20 if a.height() / (span / 20) >= 18 else 40
        v = math.ceil(st.db_lo / step) * step
        while v <= st.db_hi + 1e-9:
            y = float(self.y_of(v))
            p.setPen(QPen(style.qcolor(style.W_CENTER) if abs(v) < 1e-9 else grid, 1))
            p.drawLine(QPointF(a.left(), y), QPointF(a.right(), y))
            p.setPen(style.qcolor(style.MUTED))
            p.drawText(QRectF(0, y - 7, self.L - 5, 14), Qt.AlignRight | Qt.AlignVCenter,
                       f"{v:.0f}".replace("-", "−"))
            v += step
        p.setPen(style.qcolor(style.MUTED))
        p.drawText(QRectF(0, 2, self.L - 5, 14), Qt.AlignRight | Qt.AlignVCenter, "dB")
        p.setPen(QPen(style.qcolor(style.BORDER), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(a)

    def peak(self) -> tuple[float, float, int]:
        """(frequency, level, curve) of the highest peak of the curves."""
        best = (0.0, spm.FLOOR_DB, 0)
        if self.levels is None:
            return best
        for c in range(self.levels.shape[0]):
            f, v = spm.peak(self.res.freqs, self.levels[c], win=self.res.window)
            if v > best[1]:
                best = (f, v, c)
        return best

    def _peak(self, p: QPainter, a: QRectF) -> None:
        f, v, c = self.peak()
        if v <= self.st.db_lo:
            return
        x, y = float(self.x_of(f)), float(self.y_of(v))
        col = self.colors()[c]
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(col, 1.5))
        p.setBrush(style.qcolor(style.W_BG))
        p.drawEllipse(QPointF(x, max(a.top(), y)), 4, 4)
        txt = f"{fmt_hz(f)} · {fmt_level(v)} dB"
        nt = spm.note(f)
        if nt:
            txt += f" · {nt}"
        p.setFont(_font(10, True))
        p.setPen(style.qcolor(style.TEXT))
        w = p.fontMetrics().horizontalAdvance(txt) + 8
        tx = min(a.right() - w, x + 8)
        ty = max(a.top() + 2, y - 18)
        p.drawText(QRectF(tx, ty, w, 14), Qt.AlignLeft | Qt.AlignVCenter, txt)

    def _legend(self, p: QPainter, a: QRectF) -> None:
        p.setFont(_font(10, True))
        x = a.left()
        if self.live:
            p.setPen(QColor(style.W_PLAY))
            p.drawText(QRectF(x, 3, 80, 16), Qt.AlignLeft | Qt.AlignVCenter, "● " + t("sp_live"))
            x += 80
        for lab, col in zip(self.res.labels, self.colors()):
            p.setPen(col)
            p.drawText(QRectF(x, 3, 60, 16), Qt.AlignLeft | Qt.AlignVCenter, "— " + lab)
            x += max(34, 14 + 8 * len(lab))

    def readout(self, x: float) -> str:
        """The frequency, note and levels of the curves at x."""
        if self.levels is None:
            return ""
        f = self.f_of(x)
        i = int(np.clip(np.searchsorted(self.res.freqs, f), 0, len(self.res.freqs) - 1))
        if i and abs(self.res.freqs[i - 1] - f) < abs(self.res.freqs[i] - f):
            i -= 1
        parts = [fmt_hz(f)]
        nt = spm.note(f)
        if nt:
            parts.append(nt)
        parts += [f"{lab} {fmt_level(self.levels[c, i])} dB" for c, lab in enumerate(self.res.labels)]
        return " · ".join(parts)

    def _hover(self, p: QPainter, a: QRectF) -> None:
        x = self.hover_x
        if x is None or not (a.left() <= x <= a.right()):
            return
        p.setRenderHint(QPainter.Antialiasing, False)
        p.setPen(QPen(style.qcolor(style.W_CURSOR), 1, Qt.DotLine))
        p.drawLine(QPointF(x, a.top()), QPointF(x, a.bottom()))
        txt = self.readout(x)
        p.setFont(_font(10))
        w = p.fontMetrics().horizontalAdvance(txt) + 10
        r = QRectF(a.right() - w - 2, 3, w, 16)
        p.fillRect(r, style.qcolor(style.PANEL))
        p.setPen(style.qcolor(style.TEXT))
        p.drawText(r, Qt.AlignCenter, txt)

    def mouseMoveEvent(self, ev) -> None:
        self.hover_x = ev.position().x()
        self.update()

    def leaveEvent(self, ev) -> None:
        self.hover_x = None
        self.update()


def _reduce_points(xs: np.ndarray, ys: np.ndarray):
    """Many bins a pixel: keep each pixel column's highest point (the curve looks the same, a few hundred points)."""
    if len(xs) <= 2000:
        return xs, ys
    col = np.floor(xs).astype(np.int64)
    edges = np.flatnonzero(np.diff(col)) + 1
    starts = np.concatenate(([0], edges))
    return xs[starts], np.minimum.reduceat(ys, starts)      # the smallest y is the highest level


# ── the settings row ──────────────────────────────────────────
class SpectrumControls(QWidget):
    changed = Signal()

    def __init__(self, st: spm.Settings, compact: bool = False, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        self.fft = self._combo([(str(n), n) for n in spm.FFT_SIZES], st.fft)
        self.win = self._combo([(t("sp_w_" + w), w) for w in spm.WINDOWS], st.window)
        self.axis = self._combo([(t("sp_log"), True), (t("sp_lin"), False)], st.log_freq)
        self.range = self._combo([(f"{v} dB".replace("-", "−"), v) for v in (-60, -90, -120, -150, -180)],
                                 int(st.db_lo) if int(st.db_lo) in (-60, -90, -120, -150, -180) else -120)
        self.mode = self._combo([(t("sp_split"), "split"), (t("sp_sum"), "sum")], st.mode)
        self.smooth = self._combo([(t("sp_sm_0"), 0), ("1/3 " + t("sp_oct"), 3), ("1/6 " + t("sp_oct"), 6)],
                                  st.smooth)
        for key, cb in (("sp_fft", self.fft), ("sp_window", self.win), ("sp_axis", self.axis),
                        ("sp_range", self.range), ("sp_channels", self.mode), ("sp_smooth", self.smooth)):
            if not compact:
                lab = QLabel(t(key))
                lab.setObjectName("hint")
                h.addWidget(lab)
            cb.setToolTip(t(key))
            h.addWidget(cb)
        h.addStretch(1)
        self._top = st.db_hi

    def _combo(self, items, cur) -> QComboBox:
        cb = QComboBox()
        for text, data in items:
            cb.addItem(text, data)
        i = cb.findData(cur)
        cb.setCurrentIndex(max(0, i))
        cb.currentIndexChanged.connect(lambda _i: self.changed.emit())
        return cb

    def settings(self) -> spm.Settings:
        return spm.Settings(fft=int(self.fft.currentData()), window=self.win.currentData(),
                            log_freq=bool(self.axis.currentData()), db_lo=float(self.range.currentData()),
                            db_hi=self._top, mode=self.mode.currentData(), smooth=int(self.smooth.currentData()))


class _Runner(QObject):
    """A spectrum computed in a thread; done(run, Result | None, error)."""
    done = Signal(int, object, str)
    progress = Signal(float)

    def __init__(self):
        super().__init__()
        self.run = 0
        self.prog = None

    def start(self, d, a: int, b: int, st: spm.Settings) -> int:
        self.cancel()
        self.run += 1
        run = self.run
        prog = media.Progress(lambda f: self.progress.emit(f))
        self.prog = prog

        def work():
            try:
                self.done.emit(run, spm.spectrum(d, a, b, st, prog), "")
            except media.Cancelled:
                self.done.emit(run, None, "cancel")
            except Exception as e:                       # noqa: BLE001 — the file closed meanwhile …
                self.done.emit(run, None, str(e) or type(e).__name__)
        threading.Thread(target=work, daemon=True).start()
        return run

    def cancel(self) -> None:
        if self.prog is not None:
            self.prog.cancel()
            self.prog = None

    @property
    def busy(self) -> bool:
        return self.prog is not None


def _live(win, plot: SpectrumPlot, st: spm.Settings, prev):
    """The spectrum of what the player plays now (None when it does not)."""
    pl = win.player
    x = pl.tap(st.fft) if pl.state == "play" else None
    if x is None:
        return None
    labels = list(pl.doc.labels) if pl.doc is not None else None
    res = spm.of_frames(x, pl.doc.rate, st, labels)
    if prev is not None and prev.power.shape == res.power.shape:
        res.power = 0.55 * res.power + 0.45 * prev.power          # a little steadier to read
    return res


# ── the panel at the bottom of the window ─────────────────────
class SpectrumPanel(QWidget):
    """The spectrum of the selection (without one: an FFT at the cursor); while playing, the live spectrum."""
    closeRequested = Signal()
    windowRequested = Signal()

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.setObjectName("spectrumPanel")
        self.st = spm.Settings.from_dict(win.cfg.get("spectrum_panel"))
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 4, 6, 4)
        v.setSpacing(3)
        head = QHBoxLayout()
        head.setSpacing(6)
        cap = QLabel(t("sp_panel"))
        cap.setObjectName("caption")
        head.addWidget(cap)
        self.what = QLabel("")
        self.what.setObjectName("hint")
        head.addWidget(self.what)
        head.addSpacing(10)
        self.controls = SpectrumControls(self.st, compact=True)
        self.controls.changed.connect(self._settings_changed)
        head.addWidget(self.controls, 1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedWidth(120)
        self.bar.setTextVisible(False)
        self.bar.hide()
        head.addWidget(self.bar)
        self.cancel_b = self._tool("fa6s.xmark", t("cancel"), self.cancel)
        self.cancel_b.hide()
        head.addWidget(self.cancel_b)
        head.addWidget(self._tool("fa6s.up-right-from-square", t("sp_open_window"), self.windowRequested.emit))
        head.addWidget(self._tool("fa6s.chevron-down", t("sp_hide"), self.closeRequested.emit))
        v.addLayout(head)
        self.plot = SpectrumPlot()
        self.plot.setMinimumHeight(150)
        self.plot.set_settings(self.st)
        v.addWidget(self.plot, 1)
        self.runner = _Runner()
        self.runner.done.connect(self._done)
        self.runner.progress.connect(lambda f: self.bar.setValue(int(f * 1000)))
        self.key = None
        self._want = None
        self._live_prev = None
        self._live_t = 0.0
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(PANEL_DELAY)
        self.timer.timeout.connect(self._start)
        self.setMinimumHeight(190)

    def _tool(self, icon: str, tip: str, slot) -> QToolButton:
        b = QToolButton()
        b.setObjectName("ctl")
        b.setIcon(style.icon(icon))
        b.setToolTip(tip)
        b.clicked.connect(lambda _c=False: slot())
        return b

    def _settings_changed(self) -> None:
        self.st = self.controls.settings()
        self.win.cfg["spectrum_panel"] = self.st.to_dict()
        self.plot.set_settings(self.st)
        self._live_prev = None
        self.key = None
        self.request()

    def range_of(self, d) -> tuple[int, int, str]:
        if d.sel:
            a, b = d.sel
            return a, b, t("sp_of_sel", fmt_time(a, d.rate), fmt_time(b - a, d.rate))
        a = min(d.cursor, max(0, d.frames - self.st.fft))
        return a, min(d.frames, a + self.st.fft), t("sp_at_cursor", fmt_time(d.cursor, d.rate))

    def request(self) -> None:
        """The selection / the file changed: compute again shortly (unless it is the same)."""
        if not self.isVisible():
            return
        d = self.win.doc
        if d is None or d.frames <= 0:
            self.runner.cancel()
            self.key = None
            self._idle()
            self.what.setText("")
            self.plot.set_result(None, message=t("sp_nothing"))
            return
        if self.win.player.state == "play":
            return
        a, b, what = self.range_of(d)
        key = (d.folder, d.version, a, b, self.st.fft, self.st.window, self.st.mode)
        if key == self.key:
            return
        self._want = (key, d, a, b, what)
        self.timer.start()

    def _start(self) -> None:
        if self._want is None:
            return
        key, d, a, b, what = self._want
        self._want = None
        if d is not self.win.doc or key[1] != d.version:
            return
        self.key = key
        self.what.setText(what)
        if b - a > 10 * d.rate:                  # long: show the progress
            self.bar.setValue(0)
            self.bar.show()
            self.cancel_b.show()
        self.runner.start(d, a, b, self.st)

    def _done(self, run: int, res, err: str) -> None:
        if run != self.runner.run:
            return
        self.runner.prog = None
        self._idle()
        if res is not None and self.win.player.state != "play":
            self.plot.set_result(res)
        elif err == "cancel":
            self.key = None
            self.plot.set_result(None, message=t("cancelled"))

    def _idle(self) -> None:
        self.bar.hide()
        self.cancel_b.hide()

    def cancel(self) -> None:
        self.runner.cancel()
        self._idle()

    def tick(self) -> None:
        """From the window's timer: the live spectrum while playing (a dozen times a second)."""
        if not self.isVisible():
            return
        pl = self.win.player
        if pl.state != "play":
            if self._live_prev is not None:          # just stopped: back to the selection
                self._live_prev = None
                self.key = None
                self.request()
            return
        import time
        now = time.monotonic()
        if now - self._live_t < 0.07:
            return
        self._live_t = now
        if self.runner.busy:
            self.cancel()
        res = _live(self.win, self.plot, self.st, self._live_prev)
        if res is not None:
            self._live_prev = res
            self.what.setText(t("sp_playing"))
            self.plot.set_result(res, live=True)

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        self.key = None
        QTimer.singleShot(0, self.request)


# ── Tools → Spectrum ──────────────────────────────────────────
class SpectrumDialog(QDialog):
    """A larger spectrum of the selection (or the whole file), with all the settings; copy / save CSV."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle(t("sp_title"))
        self.setModal(False)
        self.resize(1000, 600)
        self.st = spm.Settings.from_dict(win.cfg.get("spectrum"))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        from .dialogs import _head
        lay.addWidget(_head(t("sp_title")))
        self.what = QLabel("")
        self.what.setObjectName("hint")
        lay.addWidget(self.what)
        self.controls = SpectrumControls(self.st)
        self.controls.changed.connect(self._settings_changed)
        lay.addWidget(self.controls)
        self.plot = SpectrumPlot()
        self.plot.set_settings(self.st)
        lay.addWidget(self.plot, 1)
        self.peak_l = QLabel("")
        self.peak_l.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.peak_l)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.hide()
        lay.addWidget(self.bar)
        bb = QDialogButtonBox()
        self.refresh_b = bb.addButton(t("stats_refresh"), QDialogButtonBox.ActionRole)
        self.refresh_b.setObjectName("primary")
        self.cancel_b = bb.addButton(t("cancel"), QDialogButtonBox.ActionRole)
        self.cancel_b.hide()
        self.copy_b = bb.addButton(t("sp_copy"), QDialogButtonBox.ActionRole)
        self.csv_b = bb.addButton(t("sp_csv") + "…", QDialogButtonBox.ActionRole)
        close = bb.addButton(t("close"), QDialogButtonBox.RejectRole)
        self.refresh_b.clicked.connect(self.refresh)
        self.cancel_b.clicked.connect(self.cancel)
        self.copy_b.clicked.connect(self.copy)
        self.csv_b.clicked.connect(self.save_csv)
        close.clicked.connect(self.close)
        lay.addWidget(bb)
        self.runner = _Runner()
        self.runner.done.connect(self._done)
        self.runner.progress.connect(lambda f: self.bar.setValue(int(f * 1000)))
        self.result: spm.Result | None = None
        self._live_prev = None
        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def _settings_changed(self) -> None:
        self.st = self.controls.settings()
        self.win.cfg["spectrum"] = self.st.to_dict()
        self.plot.set_settings(self.st)
        self._live_prev = None
        if self.result is not None and self.win.player.state != "play":
            self.refresh()
        self._show_peak()

    def refresh(self) -> None:
        d = self.win.doc
        self.cancel()
        if d is None or d.frames <= 0:
            self.what.setText(t("stats_nothing"))
            self.plot.set_result(None, message=t("sp_nothing"))
            self.result = None
            return
        a, b = d.sel if d.sel else (0, d.frames)
        rng = t("stats_sel", fmt_time(a, d.rate), fmt_time(b, d.rate), fmt_time(b - a, d.rate)) if d.sel else \
            t("stats_whole", fmt_time(d.frames, d.rate))
        self.what.setText(f"{d.name} · {rng}")
        self.bar.setValue(0)
        self.bar.show()
        self.cancel_b.show()
        self.refresh_b.setEnabled(False)
        self.runner.start(d, a, b, self.st)

    def cancel(self) -> None:
        self.runner.cancel()
        self.bar.hide()
        self.cancel_b.hide()
        self.refresh_b.setEnabled(True)

    def _done(self, run: int, res, err: str) -> None:
        if run != self.runner.run:
            return
        self.runner.prog = None
        self.cancel()
        if res is not None:
            self.result = res
            self.plot.set_result(res)
            self._show_peak()
        elif err and err != "cancel":
            self.what.setText(t("op_failed", err))

    def _show_peak(self) -> None:
        if self.plot.levels is None:
            self.peak_l.setText("")
            return
        f, v, c = self.plot.peak()
        nt = spm.note(f)
        res = self.plot.res
        self.peak_l.setText(t("sp_peak", fmt_hz(f), fmt_level(v), res.labels[c], nt or "—", res.segments,
                              f"{res.rate / res.fft:.2f}"))

    def _tick(self) -> None:
        if self.win.player.state == "play":
            res = _live(self.win, self.plot, self.st, self._live_prev)
            if res is not None:
                self._live_prev = res
                self.plot.set_result(res, live=True)
                self._show_peak()
        elif self._live_prev is not None:
            self._live_prev = None
            self.plot.set_result(self.result)
            self._show_peak()

    def text(self, sep: str = "\t") -> str:
        res = self.plot.res
        if res is None:
            return ""
        txt = spm.to_csv(res, self.st.smooth)
        return txt.replace(",", sep) if sep != "," else txt

    def copy(self) -> None:
        txt = self.text("\t")
        if txt:
            QApplication.clipboard().setText(txt)
            self.win._toast(t("sp_copied"))

    def save_csv(self, path: str | None = None) -> bool:
        if self.plot.res is None:
            return False
        if path is None:
            import os
            d = self.win.doc
            stem = os.path.splitext(d.name)[0] if d else "spectrum"
            path, _ = QFileDialog.getSaveFileName(self, t("sp_csv"), os.path.join(
                self.win.cfg.get("save_dir") or os.path.expanduser("~"), stem + " spectrum.csv"), "CSV (*.csv)")
            if not path:
                return False
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(self.text(","))
        self.win._toast(t("saved", path))
        return True

    def closeEvent(self, ev) -> None:
        self.cancel()
        self.timer.stop()
        super().closeEvent(ev)


# ── the spectrogram's settings ────────────────────────────────
class SpectrogramDialog(QDialog):
    """FFT size, the frequency scale, the dB range and the palettes; every change shows in the view at once
    (Cancel puts the old ones back)."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.old = sg.Settings.from_dict(win.cfg.get("spectrogram"))
        self.setWindowTitle(t("sgd_title"))
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        from .dialogs import _head
        lay.addWidget(_head(t("sgd_title")))
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        st = self.old
        self.fft = QComboBox()
        for n in sg.FFT_SIZES:
            self.fft.addItem(t("sgd_fft_n", n, f"{n / 48:.1f}"), n)
        self.fft.setCurrentIndex(max(0, self.fft.findData(st.fft)))
        form.addRow(t("sgd_fft"), self.fft)
        self.scale = QComboBox()
        for k in sg.SCALES:
            self.scale.addItem(t("sgd_" + k), k)
        self.scale.setCurrentIndex(max(0, self.scale.findData(st.scale)))
        form.addRow(t("sgd_scale"), self.scale)
        row = QHBoxLayout()
        self.lo = QDoubleSpinBox()
        self.lo.setRange(-180, -20)
        self.lo.setDecimals(0)
        self.lo.setSingleStep(5)
        self.lo.setSuffix(" dB")
        self.lo.setValue(st.db_lo)
        self.hi = QDoubleSpinBox()
        self.hi.setRange(-60, 6)
        self.hi.setDecimals(0)
        self.hi.setSingleStep(5)
        self.hi.setSuffix(" dB")
        self.hi.setValue(st.db_hi)
        row.addWidget(self.lo)
        row.addWidget(QLabel("…"))
        row.addWidget(self.hi)
        row.addStretch(1)
        form.addRow(t("sgd_range"), row)
        self.pal_dark = self._palette(st.palette_dark)
        self.pal_light = self._palette(st.palette_light)
        form.addRow(t("sgd_pal_dark"), self.pal_dark)
        form.addRow(t("sgd_pal_light"), self.pal_light)
        lay.addLayout(form)
        note = QLabel(t("sgd_note"))
        note.setObjectName("hint")
        note.setWordWrap(True)
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setObjectName("primary")
        bb.button(QDialogButtonBox.Cancel).setText(t("cancel"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        for w in (self.fft, self.scale, self.pal_dark, self.pal_light):
            w.currentIndexChanged.connect(lambda _i: self._apply())
        for w in (self.lo, self.hi):
            w.valueChanged.connect(lambda _v: self._apply())

    def _palette(self, cur: str) -> QComboBox:
        cb = QComboBox()
        for k in sg.PALETTES:
            cb.addItem(t("pal_" + k), k)
        cb.setCurrentIndex(max(0, cb.findData(cur)))
        return cb

    def settings(self) -> sg.Settings:
        lo, hi = self.lo.value(), self.hi.value()
        if hi < lo + 20:
            hi = lo + 20
        return sg.Settings(fft=int(self.fft.currentData()), scale=self.scale.currentData(), db_lo=lo, db_hi=hi,
                           palette_dark=self.pal_dark.currentData(), palette_light=self.pal_light.currentData())

    def _apply(self) -> None:
        self.win.set_spectrogram(self.settings(), save=False)

    def accept(self) -> None:
        self.win.set_spectrogram(self.settings(), save=True)
        super().accept()

    def reject(self) -> None:
        self.win.set_spectrogram(self.old, save=False)
        super().reject()
