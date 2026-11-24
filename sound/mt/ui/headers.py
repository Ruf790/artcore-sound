"""The track headers beside the timeline (3.0): per track its colour, name, mute / solo, record arm and input
monitoring, polarity, the envelopes' switch, the effects button, volume and pan, and a small level meter. They sit
at the height of their lanes and scroll with them. MiniMeter is the meter of the headers and the mixer's strips."""
from __future__ import annotations

import time

import numpy as np
from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QSizePolicy, QSlider, QToolButton, QVBoxLayout,
                               QWidget)

from ... import style
from ...i18n import t
from ...waveview import RULER_H
from .timeline import lanes, track_color

HEADER_W = 250
FLOOR = -60.0


def db_text(db: float) -> str:
    return "−∞" if db <= -60 else f"{db:+.1f}".replace("-", "−")


def pan_text(p: float) -> str:
    if abs(p) < 0.005:
        return "C"
    return f"{'L' if p < 0 else 'R'}{abs(p) * 100:.0f}"


class MiniMeter(QWidget):
    """Thin bars per channel, −60…0 dBFS, falling back at 24 dB/s, a red top after a sample at full scale."""

    def __init__(self, channels: int = 2, vertical: bool = True, parent=None):
        super().__init__(parent)
        self.vertical = vertical
        self.set_channels(channels)
        self._last = time.monotonic()
        self.setSizePolicy(QSizePolicy.Fixed if vertical else QSizePolicy.Expanding,
                           QSizePolicy.Expanding if vertical else QSizePolicy.Fixed)

    def set_channels(self, n: int) -> None:
        n = max(1, int(n))
        self.n = n
        self.level = np.full(n, FLOOR)
        self.clip = np.zeros(n, bool)
        if self.vertical:
            self.setFixedWidth(max(6, 4 * n + 2))
        else:
            self.setFixedHeight(max(5, 3 * n + 1))
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(4 * self.n + 2, 60) if self.vertical else QSize(80, 3 * self.n + 1)

    def feed(self, peaks) -> None:
        now = time.monotonic()
        dt = min(0.2, now - self._last)
        self._last = now
        if peaks is None:
            db = np.full(self.n, FLOOR)
        else:
            v = np.asarray(peaks, np.float64).ravel()
            if len(v) != self.n:
                v = np.resize(v, self.n) if len(v) else np.zeros(self.n)
            db = np.where(v > 1e-6, 20 * np.log10(np.maximum(v, 1e-6)), FLOOR)
            self.clip |= v >= 0.9999
        new = np.maximum(db, self.level - 24 * dt)
        if not np.array_equal(new, self.level):
            self.level = new
            self.update()

    def reset(self) -> None:
        self.level[:] = FLOOR
        self.clip[:] = False
        self.update()

    def mousePressEvent(self, ev) -> None:
        self.reset()

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), style.qcolor(style.W_METER))
        n = self.n
        for i in range(n):
            lv = self.level[i]
            u = max(0.0, min(1.0, (lv - FLOOR) / -FLOOR))
            col = QColor("#ef5350") if lv > -6 else QColor("#e8c547") if lv > -18 else QColor("#2fbf71")
            if self.vertical:
                bw = (self.width() - 1) / n
                h = self.height() - 3
                r = QRectF(1 + i * bw, 3 + h * (1 - u), bw - 1, h * u)
                top = QRectF(1 + i * bw, 0, bw - 1, 2)
            else:
                bh = (self.height() - 1) / n
                w = self.width()
                r = QRectF(0, 1 + i * bh, (w - 3) * u, bh - 1)
                top = QRectF(w - 2, 1 + i * bh, 2, bh - 1)
            if u > 0:
                p.fillRect(r, col)
            if self.clip[i]:
                p.fillRect(top, QColor(style.W_CLIP))


class _Btn(QToolButton):
    def __init__(self, text: str, tip: str, color: str = "", parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setToolTip(tip)
        self.setCheckable(True)
        self.setObjectName("mtBtn")
        self.setFixedSize(22, 20)
        self.color = color
        self.toggled.connect(self._paint_state)
        self._paint_state(False)

    def _paint_state(self, on: bool) -> None:
        if on and self.color:
            self.setStyleSheet(f"QToolButton#mtBtn {{ background: {self.color}; color: #101010; font-weight: 700; "
                               f"border: 1px solid {self.color}; }}")
        else:
            self.setStyleSheet("")


class TrackHeader(QWidget):
    """One track's header. changed(track id, field, value, merge key) — the view applies it."""
    changed = Signal(int, str, object, str)
    action = Signal(int, str)                  # fx | menu | arm | monitor | select

    def __init__(self, track, parent=None):
        super().__init__(parent)
        self.tid = track.id
        self.kind = track.kind
        self._lock = False
        self.setObjectName("mtHeader")
        self.setAutoFillBackground(True)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 4, 0)
        root.setSpacing(4)
        self.strip = QWidget()
        self.strip.setFixedWidth(5)
        root.addWidget(self.strip)
        col = QVBoxLayout()
        col.setContentsMargins(0, 3, 0, 3)
        col.setSpacing(3)
        row = QHBoxLayout()
        row.setSpacing(2)
        self.name = QLabel()
        self.name.setObjectName("mtName")
        self.name.setMinimumWidth(40)
        self.name.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.edit = QLineEdit()
        self.edit.hide()
        self.edit.editingFinished.connect(self._renamed)
        row.addWidget(self.name, 1)
        row.addWidget(self.edit, 1)
        self.b_mute = _Btn("M", t("mt_mute"), "#ffb02e")
        self.b_solo = _Btn("S", t("mt_solo"), "#37d67a")
        self.b_mute.toggled.connect(lambda v: self._emit("mute", v))
        self.b_solo.toggled.connect(lambda v: self._emit("solo", v))
        row.addWidget(self.b_mute)
        row.addWidget(self.b_solo)
        self.b_arm = self.b_mon = None
        if track.kind == "audio":
            self.b_arm = _Btn("●", t("mt_arm"), "#e5484d")
            self.b_mon = _Btn("I", t("mt_monitor"), "#4dabf7")
            self.b_arm.toggled.connect(lambda v: self.action.emit(self.tid, "arm_on" if v else "arm_off"))
            self.b_mon.toggled.connect(lambda v: self.action.emit(self.tid, "mon_on" if v else "mon_off"))
            row.addWidget(self.b_arm)
            row.addWidget(self.b_mon)
        col.addLayout(row)
        row2 = QHBoxLayout()
        row2.setSpacing(2)
        self.b_phase = _Btn("Ø", t("mt_phase"), "#b07cff")
        self.b_phase.toggled.connect(lambda v: self._emit("phase", v))
        self.b_env = _Btn("A", t("mt_env_show"), "#ffd43b")
        self.b_env.toggled.connect(lambda v: self.action.emit(self.tid, "env_on" if v else "env_off"))
        self.b_fx = QToolButton()
        self.b_fx.setObjectName("mtBtn")
        self.b_fx.setText("FX")
        self.b_fx.setToolTip(t("mt_fx_tip"))
        self.b_fx.setFixedHeight(20)
        self.b_fx.clicked.connect(lambda: self.action.emit(self.tid, "fx"))
        self.b_menu = QToolButton()
        self.b_menu.setObjectName("mtBtn")
        self.b_menu.setIcon(style.icon("fa6s.ellipsis"))
        self.b_menu.setFixedSize(22, 20)
        self.b_menu.setToolTip(t("mt_track_menu"))
        self.b_menu.clicked.connect(lambda: self.action.emit(self.tid, "menu"))
        row2.addWidget(self.b_phase)
        row2.addWidget(self.b_env)
        row2.addWidget(self.b_fx)
        row2.addStretch(1)
        row2.addWidget(self.b_menu)
        col.addLayout(row2)
        row3 = QHBoxLayout()
        row3.setSpacing(4)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(-600, 120)
        self.vol.setToolTip(t("mt_volume"))
        self.vol.valueChanged.connect(lambda v: self._emit("gain_db", v / 10, "hdr_gain"))
        self.vol_l = QLabel()
        self.vol_l.setObjectName("hint")
        self.vol_l.setFixedWidth(40)
        self.pan = QSlider(Qt.Horizontal)
        self.pan.setRange(-100, 100)
        self.pan.setFixedWidth(52)
        self.pan.setToolTip(t("mt_pan"))
        self.pan.valueChanged.connect(lambda v: self._emit("pan", v / 100, "hdr_pan"))
        self.pan_l = QLabel()
        self.pan_l.setObjectName("hint")
        self.pan_l.setFixedWidth(28)
        row3.addWidget(self.vol, 1)
        row3.addWidget(self.vol_l)
        row3.addWidget(self.pan)
        row3.addWidget(self.pan_l)
        col.addLayout(row3)
        self.rows2 = (row2, row3)
        root.addLayout(col, 1)
        self.meter = MiniMeter(track.channels if track.kind == "audio" else 2)
        root.addWidget(self.meter)
        self.set_track(track)

    def _emit(self, field: str, v, merge: str = "") -> None:
        if not self._lock:
            self.changed.emit(self.tid, field, v, merge)

    def set_track(self, tr, armed: bool = False, monitored: bool = False, env: bool = False,
                  current: bool = False) -> None:
        self._lock = True
        try:
            self.name.setText(tr.name)
            tip = {"audio": t("mt_audio_track_n", tr.channels), "bus": t("mt_bus")}.get(tr.kind, "")
            self.name.setToolTip(tip)
            self.strip.setStyleSheet(f"background: {track_color(tr.color).name()};")
            self.b_mute.setChecked(tr.mute)
            self.b_solo.setChecked(tr.solo)
            self.b_phase.setChecked(tr.phase)
            self.b_env.setChecked(env)
            if self.b_arm is not None:
                self.b_arm.setChecked(armed)
                self.b_mon.setChecked(monitored)
            n = len([s for s in tr.fx if not s.bypass])
            self.b_fx.setText(f"FX {n}" if tr.fx else "FX")
            self.b_fx.setStyleSheet("QToolButton#mtBtn { color: %s; font-weight: 700; }" % style.ACCENT if n else "")
            if not self.vol.isSliderDown():
                self.vol.setValue(int(round(tr.gain_db * 10)))
            self.vol_l.setText(db_text(tr.gain_db))
            self.vol.setEnabled(not tr.vol_env or not tr.env_on)
            self.pan.setEnabled((not tr.pan_env or not tr.env_on) and tr.channels <= 2)
            if not self.pan.isSliderDown():
                self.pan.setValue(int(round(tr.pan * 100)))
            self.pan_l.setText(pan_text(tr.pan))
            if self.property("current") != current:
                self.setProperty("current", current)
                self.style().unpolish(self)
                self.style().polish(self)
            if self.meter.n != (tr.channels if tr.kind == "audio" else self.meter.n):
                self.meter.set_channels(tr.channels)
        finally:
            self._lock = False

    def set_compact(self, h: int) -> None:
        small = h < 70
        for lay in self.rows2[:1]:
            for i in range(lay.count()):
                w = lay.itemAt(i).widget()
                if w is not None:
                    w.setVisible(not small)
        for i in range(self.rows2[1].count()):
            w = self.rows2[1].itemAt(i).widget()
            if w is not None:
                w.setVisible(h >= 54)

    def mouseDoubleClickEvent(self, ev) -> None:
        if ev.position().y() < 26:
            self.start_rename()

    def mousePressEvent(self, ev) -> None:
        self.action.emit(self.tid, "select")
        if ev.button() == Qt.RightButton:
            self.action.emit(self.tid, "menu")

    def start_rename(self) -> None:
        self.edit.setText(self.name.text())
        self.name.hide()
        self.edit.show()
        self.edit.setFocus()
        self.edit.selectAll()

    def _renamed(self) -> None:
        if not self.edit.isVisible():
            return
        name = self.edit.text().strip()
        self.edit.hide()
        self.name.show()
        if name and name != self.name.text():
            self.changed.emit(self.tid, "name", name, "")


class HeaderColumn(QWidget):
    """The headers at the heights of their lanes; the corner above holds the add-track buttons."""
    changed = Signal(int, str, object, str)
    action = Signal(int, str)
    addRequested = Signal(str)

    def __init__(self, timeline, parent=None):
        super().__init__(parent)
        self.tl = timeline
        self.p = None
        self.headers: dict = {}
        self.setFixedWidth(HEADER_W)
        self.setObjectName("mtHeaders")
        self.setAutoFillBackground(True)
        self.corner = QWidget(self)
        h = QHBoxLayout(self.corner)
        h.setContentsMargins(6, 6, 6, 6)
        h.setSpacing(4)
        for kind, icon, tip in (("audio", "fa6s.plus", t("mt_add_track")), ("bus", "fa6s.code-merge", t("mt_add_bus"))):
            b = QToolButton()
            b.setObjectName("ctlText")
            b.setIcon(style.icon(icon))
            b.setText(t("mt_track_short") if kind == "audio" else t("mt_bus_short"))
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, k=kind: self.addRequested.emit(k))
            h.addWidget(b)
        h.addStretch(1)

    def set_project(self, p) -> None:
        if p is not self.p:
            for w in self.headers.values():
                w.hide()
                w.setParent(None)
                w.deleteLater()
            self.headers = {}
        self.p = p
        self.sync()

    def sync(self) -> None:
        p = self.p
        if p is None:
            return
        ids = {tr.id for tr in p.tracks}
        for tid in [k for k in self.headers if k not in ids]:
            w = self.headers.pop(tid)
            w.hide()
            w.setParent(None)
            w.deleteLater()
        armed = set(p.ui.get("armed") or [])
        mon = set(p.ui.get("monitor") or [])
        env = set(p.ui.get("env") or [])
        cur = p.ui.get("current")
        for tr, top, h in lanes(p):
            w = self.headers.get(tr.id)
            if w is None or w.kind != tr.kind:
                if w is not None:
                    w.hide()
                    w.setParent(None)
                    w.deleteLater()
                w = TrackHeader(tr, self)
                w.changed.connect(self.changed)
                w.action.connect(self.action)
                self.headers[tr.id] = w
                w.show()
            w.set_track(tr, tr.id in armed, tr.id in mon, tr.id in env, tr.id == cur)
        self.relayout()

    def relayout(self) -> None:
        p = self.p
        if p is None:
            return
        self.corner.setGeometry(0, 0, self.width(), RULER_H)
        for tr, top, h in lanes(p):
            w = self.headers.get(tr.id)
            if w is None:
                continue
            y = RULER_H + top - self.tl.yoff
            if y + h < RULER_H or y > self.height():
                w.hide()
                continue
            w.setGeometry(0, int(y), self.width(), h - 1)
            w.set_compact(h)
            w.show()
            w.lower()
        self.corner.raise_()

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self.relayout()

    def feed(self, meters: dict) -> None:
        for tid, w in self.headers.items():
            w.meter.feed(meters.get(tid) if meters else None)

    def wheelEvent(self, ev) -> None:
        self.tl.set_yoff(self.tl.yoff - int(ev.angleDelta().y() / 120 * 60))

