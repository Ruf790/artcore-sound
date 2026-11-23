"""The Record window (0.4): the input (WASAPI devices with their channels and rate), the channels and the rate,
where the recording goes (a new file, into the current one at the cursor, in place of the selection), the input's
level meters before recording (ready) and while recording, the time recorded, dropped blocks, the free disk space
and how long it lasts; Record / Pause / Stop, a marker (M), a pre-roll and a start by the level."""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QLabel, QPushButton, QRadioButton, QVBoxLayout, QWidget)

from . import record, style, winint
from .doc import fmt_time
from .i18n import fmt_bytes, t
from .waveview import Meters

RATES = (22050, 32000, 44100, 48000, 88200, 96000, 176400, 192000)


class RecordDialog(QDialog):
    recorded = Signal(object)         # {"src", "target", "doc", "a", "b", "markers", "rate", "channels"}

    def __init__(self, win):
        super().__init__(win)
        from .dialogs import _head, _hint
        self.win = win
        self.rec = record.Recorder()
        from .player import InputMonitor
        self.monitor = InputMonitor(win.cfg.get("device", ""), win.cfg.get("latency", "low"), win.volume)
        self.cfg = win.cfg.setdefault("record", {}) if isinstance(win.cfg.get("record"), dict) else {}
        win.cfg["record"] = self.cfg
        self._job = None              # what Record was pressed for: (target, doc, a, b)
        self._closed = False
        self.setWindowTitle(t("rec_title"))
        self.setModal(False)
        self.setMinimumWidth(640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("rec_title")))
        top = QHBoxLayout()
        left = QVBoxLayout()
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        self.dev = QComboBox()
        self.devices = record.input_devices()
        want = self.cfg.get("device", "")
        cur = 0
        for i, dv in enumerate(self.devices):
            name = t("rec_null") if dv.get("null") else dv["name"]
            self.dev.addItem(f"{name} — {t('rec_dev_info', dv['channels'], dv['rate'])}", i)
            if dv["name"] == want or (not want and dv["default"]):
                cur = i
        if not self.devices:
            self.dev.addItem(t("rec_no_devices"), -1)
        self.dev.setCurrentIndex(cur)
        form.addRow(t("rec_device"), self.dev)
        self.chans = QComboBox()
        form.addRow(t("rec_channels"), self.chans)
        self.rate = QComboBox()
        form.addRow(t("rec_rate"), self.rate)
        where = QWidget()
        wv = QVBoxLayout(where)
        wv.setContentsMargins(0, 0, 0, 0)
        wv.setSpacing(2)
        self.target = QButtonGroup(self)
        self.t_new = QRadioButton(t("rec_to_new"))
        self.t_ins = QRadioButton(t("rec_to_cursor"))
        self.t_rep = QRadioButton(t("rec_to_sel"))
        for i, b in enumerate((self.t_new, self.t_ins, self.t_rep)):
            self.target.addButton(b, i)
            wv.addWidget(b)
        self.t_new.setChecked(True)
        form.addRow(t("rec_where"), where)
        pre = QHBoxLayout()
        self.pre = QDoubleSpinBox()
        self.pre.setRange(0, 30)
        self.pre.setDecimals(1)
        self.pre.setSuffix(" " + t("seconds"))
        self.pre.setValue(float(self.cfg.get("pre_roll", 0.0)))
        pre.addWidget(self.pre)
        pre.addStretch(1)
        form.addRow(t("rec_pre"), pre)
        trig = QHBoxLayout()
        self.trig_on = QCheckBox(t("rec_trigger"))
        self.trig_on.setChecked(bool(self.cfg.get("trigger_on", False)))
        self.trig = QDoubleSpinBox()
        self.trig.setRange(-80, 0)
        self.trig.setDecimals(0)
        self.trig.setSuffix(" dBFS")
        self.trig.setValue(float(self.cfg.get("trigger", -40.0)))
        trig.addWidget(self.trig_on)
        trig.addWidget(self.trig)
        trig.addStretch(1)
        form.addRow("", trig)
        self.mon_on = QCheckBox(t("rec_monitor"))
        self.mon_on.setToolTip(t("rec_monitor_tip"))
        self.mon_on.setChecked(bool(self.cfg.get("monitor", False)))      # off by default: no howling
        form.addRow("", self.mon_on)
        left.addLayout(form)
        self.state_l = QLabel("")
        self.state_l.setObjectName("recOn")
        left.addWidget(self.state_l)
        self.time_l = QLabel(fmt_time(0, 48000))
        self.time_l.setObjectName("recTime")
        left.addWidget(self.time_l)
        self.info_l = QLabel("")
        self.info_l.setObjectName("hint")
        left.addWidget(self.info_l)
        self.disk_l = QLabel("")
        self.disk_l.setObjectName("hint")
        left.addWidget(self.disk_l)
        left.addStretch(1)
        top.addLayout(left, 1)
        self.meters = Meters()
        self.meters.setMinimumHeight(220)
        top.addWidget(self.meters)
        lay.addLayout(top, 1)
        self.note = _hint("")
        lay.addWidget(self.note)
        btns = QHBoxLayout()
        self.b_rec = QPushButton(style.icon("fa6s.circle", "#ffffff"), t("rec_record"))
        self.b_rec.setObjectName("record")
        self.b_pause = QPushButton(style.icon("fa6s.pause"), t("pause"))
        self.b_pause.setCheckable(True)
        self.b_stop = QPushButton(style.icon("fa6s.stop"), t("stop"))
        self.b_mark = QPushButton(style.icon("fa6s.flag", style.W_MARKER), t("rec_marker"))
        self.b_close = QPushButton(t("close"))
        for b in (self.b_rec, self.b_pause, self.b_stop, self.b_mark):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(self.b_close)
        lay.addLayout(btns)
        self.b_rec.clicked.connect(self.start)
        self.b_pause.clicked.connect(self.toggle_pause)
        self.b_stop.clicked.connect(self.stop)
        self.b_mark.clicked.connect(self.marker)
        self.b_close.clicked.connect(self.reject)
        sc = QShortcut(QKeySequence("M"), self)
        sc.activated.connect(self.marker)
        self.dev.currentIndexChanged.connect(lambda _i: self._device_changed())
        self.chans.currentIndexChanged.connect(lambda _i: self._reopen())
        self.rate.currentIndexChanged.connect(lambda _i: self._reopen())
        self.target.idToggled.connect(lambda _i, on: on and self._target_changed())
        self.mon_on.toggled.connect(lambda _on: self._monitor())
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._tick)
        self._lock_combos = False
        self._target_changed(open_now=False)
        self._device_changed()
        self.timer.start()

    # ── the input ─────────────────────────────────────────────
    def device(self) -> dict | None:
        i = self.dev.currentData()
        return self.devices[i] if isinstance(i, int) and 0 <= i < len(self.devices) else None

    def target_key(self) -> str:
        return ("new", "cursor", "sel")[max(0, self.target.checkedId())]

    def _target_changed(self, open_now: bool = True) -> None:
        d = self.win.doc
        self.t_ins.setEnabled(d is not None)
        self.t_rep.setEnabled(d is not None and bool(d.sel))
        if self.target_key() != "new" and (d is None or (self.target_key() == "sel" and not d.sel)):
            self.t_new.setChecked(True)
        self._fill_formats()
        if open_now:
            self._reopen()

    def _fill_formats(self) -> None:
        """The channels / rate combos: the device's, or the current file's when recording into it."""
        dv = self.device()
        d = self.win.doc
        into = self.target_key() != "new" and d is not None
        maxch = max(1, min(8, dv["channels"] if dv else 2))
        self._lock_combos = True
        cur_ch = self.chans.currentData() or int(self.cfg.get("channels", min(2, maxch)))
        cur_rate = self.rate.currentData() or int(self.cfg.get("rate", dv["rate"] if dv else 48000))
        self.chans.clear()
        self.rate.clear()
        if into:
            self.chans.addItem(self._ch_text(d.channels), d.channels)
            self.rate.addItem(f"{d.rate} Hz", d.rate)
        else:
            for n in range(1, maxch + 1):
                self.chans.addItem(self._ch_text(n), n)
            self.chans.setCurrentIndex(max(0, self.chans.findData(min(cur_ch, maxch))))
            rates = sorted(set(RATES) | ({dv["rate"]} if dv else set()))
            for r in rates:
                self.rate.addItem(f"{r} Hz", r)
            self.rate.setCurrentIndex(max(0, self.rate.findData(cur_rate)))
        self.chans.setEnabled(not into)
        self.rate.setEnabled(not into)
        self._lock_combos = False
        self.note.setText(t("rec_into_note", d.rate, d.channels) if into else "")
        self.note.setVisible(into)

    @staticmethod
    def _ch_text(n: int) -> str:
        return {1: t("ch_mono"), 2: t("ch_stereo"), 6: "5.1", 8: "7.1"}.get(n, t("ch_n", n))

    def _device_changed(self) -> None:
        self._fill_formats()
        self._reopen()

    def _reopen(self) -> None:
        if self._lock_combos or self.rec.state in ("rec", "armed", "pause"):
            return
        dv = self.device()
        ch = int(self.chans.currentData() or 2)
        rate = int(self.rate.currentData() or 48000)
        self.meters.set_channels(self._labels(ch))
        self.meters.reset()
        ok = dv is not None and self.rec.open(dv, ch, rate)
        if not ok:
            self.state_l.setText(t("rec_open_failed", self.rec.error or t("rec_no_devices")))
        self._monitor()
        self._update()

    def _monitor(self) -> None:
        """Hear the input (2.0) or not: the monitor follows the input's rate and channels."""
        self.cfg["monitor"] = self.mon_on.isChecked()
        want = self.mon_on.isChecked() and self.rec.state != "closed"
        self.rec.monitor = None
        self.monitor.close()
        if not want:
            return
        if self.monitor.open(self.rec.rate, self.rec.channels, self._labels(self.rec.channels)):
            self.rec.monitor = self.monitor
        else:
            self.note.setText(t("rec_monitor_failed", self.monitor.error))

    @staticmethod
    def _labels(ch: int) -> list[str]:
        from .ff import labels
        return labels(ch)

    # ── recording ─────────────────────────────────────────────
    def start(self) -> None:
        if self.rec.state != "ready":
            if self.rec.state == "pause":
                self.toggle_pause()
            return
        self.win.stop_play()
        d = self.win.doc
        tg = self.target_key()
        if tg == "new" or d is None:
            tg, folder, a, b = "new", self.win.session.folder(), 0, 0
        elif tg == "sel" and d.sel:
            folder, (a, b) = d.folder, d.sel
        else:
            tg, folder, a, b = "cursor", d.folder, d.cursor, d.cursor
        self._job = (tg, d if tg != "new" else None, a, b)
        self.cfg.update(device=(self.device() or {}).get("name", ""), channels=self.rec.channels,
                        rate=self.rec.rate, pre_roll=self.pre.value(), trigger_on=self.trig_on.isChecked(),
                        trigger=self.trig.value())
        self.rec.set_pre_roll(self.pre.value())
        self.rec.record(folder, 0.0, self.trig.value() if self.trig_on.isChecked() else None)
        self._update()

    def toggle_pause(self) -> None:
        if self.rec.state in ("rec", "armed"):
            self.rec.pause()
        elif self.rec.state == "pause":
            self.rec.resume()
        self._update()

    def marker(self) -> None:
        if self.rec.add_marker() is not None:
            self.win._toast(t("rec_marked", len(self.rec.markers)))
        self._update()

    def stop(self) -> None:
        if self._job is None:
            return
        tg, d, a, b = self._job
        self._job = None
        src = self.rec.stop()
        self.b_pause.setChecked(False)
        self._update()
        if src is None:
            self.win._toast(t("rec_nothing"))
            return
        info = {"src": src, "target": tg, "doc": d, "a": a, "b": b, "rate": self.rec.rate,
                "channels": self.rec.channels, "markers": list(self.rec.markers), "overflows": self.rec.overflows}
        self.recorded.emit(info)

    def recording(self) -> bool:
        return self.rec.state in ("rec", "armed", "pause")

    # ── keeping it up to date ─────────────────────────────────
    def _tick(self) -> None:
        lv = self.rec.levels()
        if lv is not None:
            self.meters.feed(lv)
        self._update(light=True)

    def _update(self, light: bool = False) -> None:
        st = self.rec.state
        rate = self.rec.rate or 48000
        names = {"ready": t("rec_ready"), "armed": t("rec_armed"), "rec": t("rec_on"), "pause": t("rec_paused"),
                 "closed": self.state_l.text() if st == "closed" else ""}
        if st != "closed":
            self.state_l.setText(("● " if st == "rec" else "") + names.get(st, ""))
        self.time_l.setText(fmt_time(self.rec.frames, rate))
        q = self.rec.queued_seconds()
        self.info_l.setText(t("rec_info", self.rec.overflows, len(self.rec.markers), f"{q:.1f}",
                              t("st_null") if self.rec.backend == "null" else self.rec.device))
        folder = self.win.session.path
        free = winint.free_space(folder)
        if free >= 0:
            per_s = rate * max(1, self.rec.channels) * 4 * 1.02
            self.disk_l.setText(t("rec_disk", fmt_bytes(free), fmt_time(free / per_s * rate, rate, False)))
        recording = st in ("rec", "armed", "pause")
        self.b_rec.setEnabled(st == "ready")
        self.b_pause.setEnabled(recording)
        self.b_pause.setChecked(st == "pause")
        self.b_stop.setEnabled(recording)
        self.b_mark.setEnabled(st in ("rec", "pause"))
        if not light:
            for w in (self.dev, self.t_new, self.t_ins, self.t_rep, self.pre, self.trig_on, self.trig):
                w.setEnabled(not recording)
            if not recording:
                d = self.win.doc
                self.t_ins.setEnabled(d is not None)
                self.t_rep.setEnabled(d is not None and bool(d.sel))
                into = self.target_key() != "new" and d is not None
                self.chans.setEnabled(not into)
                self.rate.setEnabled(not into)
            else:
                self.chans.setEnabled(False)
                self.rate.setEnabled(False)
        self.win._update_transport()

    def _cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.recording():
            self.stop()
        self.timer.stop()
        self.rec.monitor = None
        self.monitor.close()
        self.rec.close()
        self.win.cfg["record"] = self.cfg

    def done(self, r: int) -> None:
        self._cleanup()
        super().done(r)

    def closeEvent(self, ev) -> None:
        self._cleanup()
        super().closeEvent(ev)
