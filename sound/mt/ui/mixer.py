"""The mixer (3.0): a strip per track and bus — where it goes, its effects (a tick bypasses one, a double click opens
it), its sends to buses, pan, fader with the level meter, mute / solo / polarity — and the master's strip on the
right. The strips change the same project the headers and the timeline show."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QScrollArea, QSizePolicy, QSlider, QToolButton, QVBoxLayout, QWidget)

from ... import style
from ...i18n import t
from ..model import MASTER
from .headers import MiniMeter, _Btn, db_text, pan_text
from .timeline import track_color

STRIP_W = 120


def slot_name(s) -> str:
    from ... import fx
    if s.key == "vst3":
        return ((s.values or {}).get("plugin") or {}).get("name") or "VST3"
    try:
        return t(fx.get(s.key).name)
    except KeyError:
        return s.key


class Strip(QFrame):
    changed = Signal(int, str, object, str)
    action = Signal(int, str)

    def __init__(self, track, parent=None):
        super().__init__(parent)
        self.tid = track.id
        self.kind = track.kind
        self._lock = False
        self.setObjectName("mtStrip")
        self.setFixedWidth(STRIP_W + (30 if track.kind == "master" else 0))
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 3, 4, 3)
        v.setSpacing(2)
        self.bar = QWidget()
        self.bar.setFixedHeight(4)
        v.addWidget(self.bar)
        self.name = QLabel()
        self.name.setObjectName("mtName")
        self.name.setAlignment(Qt.AlignCenter)
        v.addWidget(self.name)
        self.out = None
        if track.kind != "master":
            self.out = QComboBox()
            self.out.setToolTip(t("mt_output"))
            self.out.activated.connect(lambda _i: self._emit("output", self.out.currentData()))
            v.addWidget(self.out)
        fx_row = QHBoxLayout()
        cap = QLabel(t("mt_fx"))
        cap.setObjectName("caption")
        fx_row.addWidget(cap)
        fx_row.addStretch(1)
        b = QToolButton()
        b.setObjectName("mtBtn")
        b.setIcon(style.icon("fa6s.plus"))
        b.setToolTip(t("mt_fx_tip"))
        b.clicked.connect(lambda: self.action.emit(self.tid, "fx"))
        fx_row.addWidget(b)
        v.addLayout(fx_row)
        self.fx = QListWidget()
        self.fx.setObjectName("mtFx")
        self.fx.setFixedHeight(46)
        self.fx.itemChanged.connect(self._fx_toggled)
        self.fx.itemDoubleClicked.connect(lambda it: self.action.emit(self.tid, f"fx_edit:{it.data(Qt.UserRole)}"))
        v.addWidget(self.fx)
        self.sends_box = QVBoxLayout()
        self.sends_box.setSpacing(1)
        self.send_rows: dict = {}
        if track.kind != "master":
            srow = QHBoxLayout()
            cap = QLabel(t("mt_sends"))
            cap.setObjectName("caption")
            srow.addWidget(cap)
            srow.addStretch(1)
            b = QToolButton()
            b.setObjectName("mtBtn")
            b.setIcon(style.icon("fa6s.plus"))
            b.setToolTip(t("mt_send_add"))
            b.clicked.connect(lambda: self.action.emit(self.tid, "send_menu"))
            srow.addWidget(b)
            v.addLayout(srow)
            v.addLayout(self.sends_box)
        self.pan = self.pan_l = None
        if track.kind != "master":
            prow = QHBoxLayout()
            self.pan = QSlider(Qt.Horizontal)
            self.pan.setRange(-100, 100)
            self.pan.setToolTip(t("mt_pan"))
            self.pan.valueChanged.connect(lambda x: self._emit("pan", x / 100, "mx_pan"))
            self.pan_l = QLabel()
            self.pan_l.setObjectName("hint")
            self.pan_l.setFixedWidth(28)
            prow.addWidget(self.pan, 1)
            prow.addWidget(self.pan_l)
            v.addLayout(prow)
        mid = QHBoxLayout()
        mid.setSpacing(3)
        self.fader = QSlider(Qt.Vertical)
        self.fader.setRange(-600, 120)
        self.fader.setMinimumHeight(70)
        self.fader.setToolTip(t("mt_volume"))
        self.fader.valueChanged.connect(lambda x: self._emit("gain_db", x / 10, "mx_gain"))
        mid.addStretch(1)
        mid.addWidget(self.fader)
        self.meter = MiniMeter(track.channels)
        mid.addWidget(self.meter)
        mid.addStretch(1)
        v.addLayout(mid, 1)
        self.db = QLabel()
        self.db.setAlignment(Qt.AlignCenter)
        self.db.setObjectName("hint")
        v.addWidget(self.db)
        brow = QHBoxLayout()
        brow.setSpacing(2)
        self.b_mute = _Btn("M", t("mt_mute"), "#ffb02e")
        self.b_mute.toggled.connect(lambda x: self._emit("mute", x))
        brow.addWidget(self.b_mute)
        self.b_solo = self.b_phase = None
        if track.kind != "master":
            self.b_solo = _Btn("S", t("mt_solo"), "#37d67a")
            self.b_solo.toggled.connect(lambda x: self._emit("solo", x))
            self.b_phase = _Btn("Ø", t("mt_phase"), "#b07cff")
            self.b_phase.toggled.connect(lambda x: self._emit("phase", x))
            brow.addWidget(self.b_solo)
            brow.addWidget(self.b_phase)
        v.addLayout(brow)

    def _emit(self, field, v, merge: str = "") -> None:
        if not self._lock:
            self.changed.emit(self.tid, field, v, merge)

    def _fx_toggled(self, it: QListWidgetItem) -> None:
        if not self._lock:
            self.changed.emit(self.tid, "fx_bypass", (it.data(Qt.UserRole), it.checkState() != Qt.Checked), "")

    def set_track(self, tr, project) -> None:
        self._lock = True
        try:
            self.name.setText(tr.name if tr.kind != "master" else t("mt_master"))
            self.bar.setStyleSheet(f"background: {track_color(tr.color).name() if tr.kind != 'master' else style.ACCENT};")
            if self.out is not None:
                self.out.clear()
                self.out.addItem(t("mt_master"), MASTER)
                for b in project.buses():
                    if b.id != tr.id and (tr.kind == "audio" or project.can_send(tr.id, b.id)):
                        self.out.addItem(b.name, b.id)
                self.out.setCurrentIndex(max(0, self.out.findData(tr.output)))
            fk = tuple((s.id, s.key, s.bypass, slot_name(s)) for s in tr.fx)
            if fk != getattr(self, "_fk", None):
                self._fk = fk
                self.fx.clear()
                for s in tr.fx:
                    it = QListWidgetItem(slot_name(s))
                    it.setData(Qt.UserRole, s.id)
                    it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                    it.setCheckState(Qt.Unchecked if s.bypass else Qt.Checked)
                    self.fx.addItem(it)
            sk = tuple((s.bus, s.pre, getattr(project.track(s.bus), "name", "")) for s in tr.sends)
            if sk != getattr(self, "_sk", None):
                self._sk = sk
                self._sends(tr, project)
            else:
                for s in tr.sends:
                    sl = self.send_rows.get(s.bus)
                    if sl is not None and not sl.isSliderDown():
                        sl.setValue(int(round(s.db * 10)))
            if self.pan is not None:
                if not self.pan.isSliderDown():
                    self.pan.setValue(int(round(tr.pan * 100)))
                self.pan_l.setText(pan_text(tr.pan))
                self.pan.setEnabled((not tr.pan_env or not tr.env_on) and (tr.channels <= 2 or
                                                                            tr.kind == "bus"))
            if not self.fader.isSliderDown():
                self.fader.setValue(int(round(tr.gain_db * 10)))
            self.db.setText(db_text(tr.gain_db) + " dB")
            self.b_mute.setChecked(tr.mute)
            if self.b_solo is not None:
                self.b_solo.setChecked(tr.solo)
                self.b_phase.setChecked(tr.phase)
            ch = tr.channels if tr.kind == "audio" else project.channels
            if self.meter.n != ch:
                self.meter.set_channels(ch)
        finally:
            self._lock = False

    def _sends(self, tr, project) -> None:
        while self.sends_box.count():
            it = self.sends_box.takeAt(0)
            if it.layout() is not None:
                while it.layout().count():
                    w = it.layout().takeAt(0).widget()
                    if w is not None:
                        w.deleteLater()
            elif it.widget() is not None:
                it.widget().deleteLater()
        self.send_rows = {}
        if tr.kind == "master":
            return
        for s in tr.sends:
            b = project.track(s.bus)
            if b is None:
                continue
            row = QHBoxLayout()
            row.setSpacing(2)
            lab = QLabel(b.name)
            lab.setObjectName("hint")
            lab.setFixedWidth(34)
            lab.setToolTip(t("mt_send_to", b.name))
            sl = QSlider(Qt.Horizontal)
            sl.setRange(-600, 120)
            sl.setValue(int(round(s.db * 10)))
            sl.setToolTip(f"{db_text(s.db)} dB")
            self.send_rows[s.bus] = sl
            sl.valueChanged.connect(lambda x, bus=s.bus, w=sl: (w.setToolTip(f"{db_text(x / 10)} dB"),
                                                                 self._emit("send", (bus, x / 10), f"send:{bus}")))
            pre = QCheckBox()
            pre.setToolTip(t("mt_send_pre"))
            pre.setChecked(s.pre)
            pre.toggled.connect(lambda x, bus=s.bus: self._emit("send_pre", (bus, x)))
            x = QToolButton()
            x.setObjectName("mtBtn")
            x.setIcon(style.icon("fa6s.xmark"))
            x.setToolTip(t("mt_send_remove"))
            x.clicked.connect(lambda _c=False, bus=s.bus: self._emit("send_remove", bus))
            for w in (lab, sl, pre, x):
                row.addWidget(w)
            self.sends_box.addLayout(row)


class MixerPanel(QWidget):
    changed = Signal(int, str, object, str)
    action = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.p = None
        self.strips: dict = {}
        self.setObjectName("mtMixer")
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.area.setFrameShape(QFrame.NoFrame)
        self.inner = QWidget()
        self.inner.setObjectName("mtMixerInner")
        self.row = QHBoxLayout(self.inner)
        self.row.setContentsMargins(4, 4, 4, 4)
        self.row.setSpacing(4)
        self.row.addStretch(1)
        self.area.setWidget(self.inner)
        h.addWidget(self.area, 1)
        self.master_box = QHBoxLayout()
        self.master_box.setContentsMargins(4, 4, 4, 4)
        h.addLayout(self.master_box)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    @staticmethod
    def _drop(w) -> None:
        w.hide()
        w.setParent(None)
        w.deleteLater()

    def set_project(self, p) -> None:
        if p is not self.p:
            for s in self.strips.values():
                self._drop(s)
            self.strips = {}
        self.p = p
        self.sync()

    def sync(self) -> None:
        p = self.p
        if p is None:
            return
        order = list(p.tracks) + [p.master]
        ids = {x.id for x in order}
        for tid in [k for k in self.strips if k not in ids]:
            self._drop(self.strips.pop(tid))
        for i, tr in enumerate(p.tracks):
            s = self.strips.get(tr.id)
            if s is None or s.kind != tr.kind:
                if s is not None:
                    self._drop(s)
                s = self.strips[tr.id] = Strip(tr)
                s.changed.connect(self.changed)
                s.action.connect(self.action)
            self.row.insertWidget(i, s)
            s.set_track(tr, p)
        m = self.strips.get(MASTER)
        if m is None:
            m = self.strips[MASTER] = Strip(p.master)
            m.changed.connect(self.changed)
            m.action.connect(self.action)
            self.master_box.addWidget(m)
        m.set_track(p.master, p)

    def feed(self, meters: dict) -> None:
        for tid, s in self.strips.items():
            s.meter.feed(meters.get(tid) if meters else None)
