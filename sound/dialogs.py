"""Dialogs: Save as, which track to open, the small processing dialogs, Settings, the update."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QProgressBar, QPushButton, QRadioButton, QTextBrowser, QVBoxLayout, QWidget)

from . import VERSION, ff, media, style
from .doc import fmt_time, parse_time
from .i18n import t


def _head(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet("font-size: 13pt; font-weight: 700; padding-bottom: 4px;")
    return lab


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("hint")
    lab.setWordWrap(True)
    return lab


def _buttons(dlg: QDialog, ok_text: str = "") -> QDialogButtonBox:
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    ok = bb.button(QDialogButtonBox.Ok)
    ok.setObjectName("primary")
    ok.setText(ok_text or t("ok"))
    bb.button(QDialogButtonBox.Cancel).setText(t("cancel"))
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    return bb


def option_text(fmt: str, opt: str) -> str:
    if fmt in ("wav", "flac"):
        return t(f"o_{opt}")
    if fmt == "mp3":
        return t(f"o_{opt}") if opt.startswith("v") else t("o_cbr", opt)
    return t("o_kbps", opt)


class SaveDialog(QDialog):
    """File name, format and its quality; for a selection, «the whole file / the selection»."""

    def __init__(self, doc, path: str, fmt: str, option: str, parent=None, selection: bool = False):
        super().__init__(parent)
        self.doc = doc
        self.setWindowTitle(t("sv_title"))
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("sv_title")))
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        row = QHBoxLayout()
        self.name = QLineEdit(path)
        row.addWidget(self.name, 1)
        b = QPushButton(t("sv_browse"))
        b.clicked.connect(self._browse)
        row.addWidget(b)
        form.addRow(t("sv_name"), row)
        self.fmt = QComboBox()
        for k, f in media.FORMATS.items():
            self.fmt.addItem(f.name, k)
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(fmt)))
        form.addRow(t("sv_format"), self.fmt)
        self.opt = QComboBox()
        form.addRow(t("sv_option"), self.opt)
        self.what_whole = self.what_sel = None
        if doc.sel:
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            self.what_whole = QRadioButton(t("sv_whole"))
            a, b2 = doc.sel
            self.what_sel = QRadioButton(t("sv_selection", fmt_time(b2 - a, doc.rate)))
            (self.what_sel if selection else self.what_whole).setChecked(True)
            h.addWidget(self.what_whole)
            h.addWidget(self.what_sel)
            h.addStretch(1)
            form.addRow(t("sv_what"), w)
        lay.addLayout(form)
        self.note = _hint("")
        lay.addWidget(self.note)
        lay.addWidget(_buttons(self, t("save")))
        self.fmt.currentIndexChanged.connect(lambda _i: self._fill(""))
        self._fill(option)

    def _fill(self, option: str) -> None:
        k = self.format()
        f = media.FORMATS[k]
        self.opt.clear()
        for o in f.options:
            self.opt.addItem(option_text(k, o), o)
        want = option or media.default_option(k, self.doc.channels, getattr(self.doc.track, "bits", 0) or 0)
        self.opt.setCurrentIndex(max(0, self.opt.findData(want)))
        stem, ext = os.path.splitext(self.name.text())
        if ext.lower() != f.ext and stem:
            self.name.setText(stem + f.ext)
        notes = []
        ch, rate = media.out_shape(k, self.doc.channels, self.doc.rate)
        if ch < self.doc.channels:
            notes.append(t("sv_mp3_stereo") if k == "mp3" else t("sv_ac3_51"))
        if rate != self.doc.rate:
            notes.append(t("sv_rate", self.doc.rate, rate))
        if f.lossy:
            notes.append(t("sv_lossy"))
        self.note.setText("\n".join(notes))
        self.note.setVisible(bool(notes))

    def _browse(self) -> None:
        f = media.FORMATS[self.format()]
        p, _ = QFileDialog.getSaveFileName(self, t("sv_title"), self.name.text(), f"{f.name} (*{f.ext})")
        if p:
            if not p.lower().endswith(f.ext):
                p += f.ext
            self.name.setText(os.path.normpath(p))

    def format(self) -> str:
        return self.fmt.currentData() or "wav"

    def option(self) -> str:
        return self.opt.currentData() or media.FORMATS[self.format()].default

    def path(self) -> str:
        p = self.name.text().strip()
        ext = media.FORMATS[self.format()].ext
        if p and not p.lower().endswith(ext):
            p += ext
        return p

    def only_selection(self) -> bool:
        return bool(self.what_sel and self.what_sel.isChecked())


class TrackDialog(QDialog):
    """A file with several sound tracks (a film): which one to open."""

    def __init__(self, info: ff.FileInfo, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("track_title"))
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("track_title")))
        lay.addWidget(_hint(t("track_text", os.path.basename(info.path))))
        self.list = QListWidget()
        for tr in info.tracks:
            parts = [f"#{tr.pos + 1}", ff.codec_name(tr), f"{tr.rate} Hz", ff.layout_for(tr.channels, tr.layout)
                     or f"{tr.channels} ch"]
            if tr.bitrate:
                parts.append(f"{tr.bitrate // 1000} kbit/s")
            if tr.lang:
                parts.append(tr.lang)
            if tr.title:
                parts.append(f"«{tr.title}»")
            if tr.default:
                parts.append(f"({t('track_default')})")
            it = QListWidgetItem(" · ".join(parts))
            it.setData(Qt.UserRole, tr.pos)
            self.list.addItem(it)
        default = next((i for i, tr in enumerate(info.tracks) if tr.default), 0)
        self.list.setCurrentRow(default)
        self.list.itemDoubleClicked.connect(lambda _it: self.accept())
        lay.addWidget(self.list)
        lay.addWidget(_buttons(self, t("open")))

    def track_pos(self) -> int:
        it = self.list.currentItem()
        return int(it.data(Qt.UserRole)) if it else 0


class ValueDialog(QDialog):
    """A small dialog: a title, rows of fields (spin boxes, combos, time fields) and a note."""

    def __init__(self, title: str, parent=None, note: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(380)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(title))
        self.form = QFormLayout()
        self.form.setHorizontalSpacing(14)
        lay.addLayout(self.form)
        self.note = _hint(note)
        self.note.setVisible(bool(note))
        lay.addWidget(self.note)
        lay.addWidget(_buttons(self))

    def spin(self, label: str, value: float, lo: float, hi: float, suffix: str = "", decimals: int = 1,
             step: float = 0.5) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(decimals)
        s.setSingleStep(step)
        s.setValue(value)
        s.setSuffix(suffix)
        self.form.addRow(label, s)
        return s

    def combo(self, label: str, items, current=None) -> QComboBox:
        c = QComboBox()
        for text, data in items:
            c.addItem(text, data)
        if current is not None:
            c.setCurrentIndex(max(0, c.findData(current)))
        self.form.addRow(label, c)
        return c

    def time_field(self, label: str, frames: int, rate: int) -> QLineEdit:
        e = QLineEdit(fmt_time(frames, rate))
        e.setProperty("rate", rate)
        self.form.addRow(label, e)
        return e

    @staticmethod
    def frames_of(e: QLineEdit) -> int | None:
        return parse_time(e.text(), int(e.property("rate")))


CURVES = ("linear", "log", "exp", "scurve")
RATES = (8000, 11025, 16000, 22050, 32000, 44100, 48000, 88200, 96000, 176400, 192000)


class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        from . import player
        self.cfg = cfg
        self.setWindowTitle(t("set_title"))
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("set_title")))
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        self.lang = QComboBox()
        self.lang.addItem("Русский", "ru")
        self.lang.addItem("English", "en")
        self.lang.setCurrentIndex(max(0, self.lang.findData(cfg.get("language"))))
        form.addRow(t("set_lang"), self.lang)
        self.theme = QComboBox()
        for k in ("dark", "light", "auto"):
            self.theme.addItem(t(f"theme_{k}"), k)
        self.theme.setCurrentIndex(max(0, self.theme.findData(cfg.get("theme"))))
        form.addRow(t("set_theme"), self.theme)
        self.look = QComboBox()
        for k in ("classic", "modern"):
            self.look.addItem(t(f"look_{k}"), k)
        self.look.setCurrentIndex(max(0, self.look.findData(cfg.get("look"))))
        form.addRow(t("set_look"), self.look)
        self.device = QComboBox()
        self.device.addItem(t("set_device_default"), "")
        for d in player.output_devices():
            self.device.addItem(f"{d['name']} ({d['channels']} ch, {d['rate']} Hz)", d["name"])
        i = self.device.findData(cfg.get("device", ""))
        if i < 0 and cfg.get("device"):
            self.device.addItem(cfg["device"], cfg["device"])
            i = self.device.count() - 1
        self.device.setCurrentIndex(max(0, i))
        form.addRow(t("set_device"), self.device)
        self.latency = QComboBox()
        for k in ("low", "high"):
            self.latency.addItem(t(f"lat_{k}"), k)
        self.latency.setCurrentIndex(max(0, self.latency.findData(cfg.get("latency"))))
        form.addRow(t("set_latency"), self.latency)
        row = QHBoxLayout()
        self.temp = QLineEdit(cfg.get("temp_dir", ""))
        self.temp.setPlaceholderText(t("set_temp_default"))
        row.addWidget(self.temp, 1)
        b = QPushButton(t("sv_browse"))
        b.clicked.connect(self._browse)
        row.addWidget(b)
        form.addRow(t("set_temp"), row)
        lay.addLayout(form)
        lay.addWidget(_hint(t("set_temp_note")))
        self.ret = QCheckBox(t("set_return"))
        self.ret.setChecked(cfg.get("return_cursor", True))
        lay.addWidget(self.ret)
        self.upd = QCheckBox(t("set_updates"))
        self.upd.setChecked(cfg.get("check_updates", True))
        lay.addWidget(self.upd)
        lay.addWidget(_buttons(self))

    def _browse(self) -> None:
        p = QFileDialog.getExistingDirectory(self, t("set_temp"), self.temp.text())
        if p:
            self.temp.setText(os.path.normpath(p))

    def apply(self) -> None:
        self.cfg.update(language=self.lang.currentData(), theme=self.theme.currentData(), look=self.look.currentData(),
                        device=self.device.currentData() or "", latency=self.latency.currentData(),
                        temp_dir=self.temp.text().strip(), return_cursor=self.ret.isChecked(),
                        check_updates=self.upd.isChecked())


class UpdateDialog(QDialog):
    """Release notes + download and silent install of the new Setup.exe."""
    progress = Signal(int, int)
    done_ = Signal(str, str)

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        from . import updater
        self.info, self.updater, self.path = info, updater, ""
        self.setWindowTitle(t("upd_title"))
        self.resize(600, 440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.addWidget(_head(t("upd_available", info["version"])))
        sub = QLabel(t("upd_text", info["version"], VERSION))
        sub.setStyleSheet(f"color: {style.MUTED};")
        lay.addWidget(sub)
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(info.get("notes") or "")
        lay.addWidget(notes, 1)
        self.bar = QProgressBar()
        self.bar.hide()
        lay.addWidget(self.bar)
        lay.addWidget(_hint(t("upd_restart")))
        bb = QDialogButtonBox()
        self.go = bb.addButton(t("upd_install"), QDialogButtonBox.AcceptRole)
        self.go.setObjectName("primary")
        bb.addButton(t("upd_later"), QDialogButtonBox.RejectRole)
        bb.accepted.connect(self._go)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.progress.connect(self._on_progress)
        self.done_.connect(self._on_done)

    def _go(self):
        import threading
        self.go.setEnabled(False)
        self.bar.setRange(0, max(1, self.info.get("size", 0)))
        self.bar.show()

        def work():
            try:
                p = self.updater.download(self.info["url"], self.info.get("size", 0),
                                          lambda d, n: self.progress.emit(d, n))
                self.done_.emit(p, "")
            except Exception as e:
                self.done_.emit("", str(e))
        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, d: int, n: int):
        if n:
            self.bar.setMaximum(n)
        self.bar.setValue(min(d, self.bar.maximum()))
        self.bar.setFormat(t("upd_downloading", f"{d / 1048576:.1f}", f"{(n or d) / 1048576:.1f}"))

    def _on_done(self, path: str, err: str):
        from PySide6.QtWidgets import QMessageBox
        if err:
            self.go.setEnabled(True)
            QMessageBox.warning(self, t("upd_title"), t("upd_error", err))
            return
        self.path = path
        self.accept()


# ── 0.2: normalization, trim silence, stereo → mono, statistics ──────────
def fmt_db(v: float, dec: int = 1) -> str:
    """−3.2, −∞ (a real minus sign)."""
    import math
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if not math.isfinite(v):
        return "−∞" if v < 0 else "∞"
    s = f"{v:.{dec}f}"
    if s.startswith("-"):
        s = "−" + s[1:]
        if not s.strip("−0.,"):
            s = s[1:]                    # −0.0 → 0.0
    return s


class NormalizeDialog(QDialog):
    """Normalize by the peak (sample or true) or by the loudness (EBU R128 integrated) to a target, with a
    true-peak ceiling."""

    def __init__(self, cfg: dict, now: str = "", whole: bool = False, parent=None):
        super().__init__(parent)
        from . import proc
        self.cfg = cfg
        n = cfg.get("normalize") or {}
        self.setWindowTitle(t("nz_title"))
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("nz_title")))
        self.by_peak = QRadioButton(t("nz_peak"))
        self.by_lufs = QRadioButton(t("nz_lufs"))
        (self.by_lufs if n.get("mode") == "loudness" else self.by_peak).setChecked(True)
        lay.addWidget(self.by_peak)
        f1 = QFormLayout()
        f1.setContentsMargins(24, 0, 0, 6)
        self.peak = QDoubleSpinBox()
        self.peak.setRange(-60, 0)
        self.peak.setDecimals(2)
        self.peak.setSingleStep(0.1)
        self.peak.setValue(float(n.get("peak", -0.1)))
        f1.addRow(t("nz_peak_to"), self.peak)
        self.tp = QCheckBox(t("nz_peak_tp"))
        self.tp.setChecked(bool(n.get("peak_tp", False)))
        self.tp.toggled.connect(self._units)
        f1.addRow("", self.tp)
        lay.addLayout(f1)
        lay.addWidget(self.by_lufs)
        f2 = QFormLayout()
        f2.setContentsMargins(24, 0, 0, 6)
        self.target = QComboBox()
        for v, key in zip(proc.LUFS_TARGETS, ("nz_t23", "nz_t16", "nz_t14")):
            self.target.addItem(t(key), v)
        self.target.addItem(t("nz_custom"), None)
        self.custom = QDoubleSpinBox()
        self.custom.setRange(-60, 0)
        self.custom.setDecimals(1)
        self.custom.setSingleStep(0.5)
        self.custom.setSuffix(" LUFS")
        tv = n.get("target", -23.0)
        i = self.target.findData(tv)
        if i < 0:
            i = self.target.count() - 1
            self.custom.setValue(float(tv))
        else:
            self.custom.setValue(-18.0)
        self.target.setCurrentIndex(i)
        self.target.currentIndexChanged.connect(lambda _i: self._enable())
        f2.addRow(t("nz_target"), self.target)
        f2.addRow(t("nz_custom_value"), self.custom)
        self.ceiling = QDoubleSpinBox()
        self.ceiling.setRange(-20, 0)
        self.ceiling.setDecimals(1)
        self.ceiling.setSingleStep(0.5)
        self.ceiling.setSuffix(" dBTP")
        self.ceiling.setValue(float(n.get("ceiling", -1.0)))
        f2.addRow(t("nz_ceiling"), self.ceiling)
        lay.addLayout(f2)
        notes = [x for x in (now, t("p_whole") if whole else "") if x]
        lay.addWidget(_hint(t("nz_note")))
        if notes:
            self.now = _hint("\n".join(notes))
            lay.addWidget(self.now)
        lay.addWidget(_buttons(self))
        self.by_peak.toggled.connect(lambda _c: self._enable())
        self._units()
        self._enable()

    def _units(self) -> None:
        self.peak.setSuffix(" dBTP" if self.tp.isChecked() else " dBFS")

    def _enable(self) -> None:
        p = self.by_peak.isChecked()
        self.peak.setEnabled(p)
        self.tp.setEnabled(p)
        for w in (self.target, self.ceiling):
            w.setEnabled(not p)
        self.custom.setEnabled(not p and self.target.currentData() is None)

    def mode(self) -> str:
        return "peak" if self.by_peak.isChecked() else "loudness"

    def lufs_target(self) -> float:
        v = self.target.currentData()
        return float(self.custom.value() if v is None else v)

    def settings(self) -> dict:
        """The choice, also what cfg["normalize"] keeps."""
        return {"mode": self.mode(), "peak": self.peak.value(), "peak_tp": self.tp.isChecked(),
                "target": self.lufs_target(), "ceiling": self.ceiling.value()}


class StatsDialog(QDialog):
    """Tools → Statistics: a table per channel and for the whole (peak, true peak, min / max, RMS, DC offset,
    clipped samples) and the loudness (integrated, LRA, max momentary / short-term). Works on the window's current
    file and selection; «Refresh» measures again — in a thread, with a progress bar and Cancel."""
    finished_ = Signal(object, object, object, str)   # run, key, Stats, error ("" / "cancel" / text)
    progress_ = Signal(float)

    ROWS = ("st_peak", "st_tp", "st_min", "st_max", "st_rms", "st_dc", "st_clipped")
    UNITS = ("dBFS", "dBTP", "", "", "dBFS", "%", "")

    def __init__(self, window):
        super().__init__(window)
        from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget
        self.win = window
        self.key = None
        self.prog = None
        self.run = 0
        self.setWindowTitle(t("stats_title"))
        self.setModal(False)
        self.resize(640, 520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("stats_title")))
        self.what = _hint("")
        lay.addWidget(self.what)
        self.table = QTableWidget(len(self.ROWS), 1)
        self.table.setObjectName("stats")
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.ContiguousSelection)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.setVerticalHeaderLabels([f"{t(k)}, {u}" if u else t(k) for k, u in zip(self.ROWS, self.UNITS)])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setMinimumSectionSize(64)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.setSizeAdjustPolicy(QAbstractItemView.AdjustToContents)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setFocusPolicy(Qt.NoFocus)
        from PySide6.QtWidgets import QSizePolicy
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        lay.addWidget(self.table)
        cap = QLabel(t("stats_loudness"))
        cap.setObjectName("caption")
        lay.addWidget(cap)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        self.loud = {}
        for k in ("st_integrated", "st_lra", "st_momentary", "st_short", "st_threshold"):
            lab = QLabel("—")
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.loud[k] = lab
            form.addRow(t(k), lab)
        lay.addLayout(form)
        lay.addWidget(_hint(t("stats_note")))
        lay.addStretch(1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.hide()
        lay.addWidget(self.bar)
        bb = QDialogButtonBox()
        self.refresh_b = bb.addButton(t("stats_refresh"), QDialogButtonBox.ActionRole)
        self.refresh_b.setObjectName("primary")
        self.cancel_b = bb.addButton(t("cancel"), QDialogButtonBox.ActionRole)
        self.cancel_b.hide()
        close = bb.addButton(t("close"), QDialogButtonBox.RejectRole)
        self.refresh_b.clicked.connect(self.refresh)
        self.cancel_b.clicked.connect(self.cancel)
        close.clicked.connect(self.close)
        lay.addWidget(bb)
        self.finished_.connect(self._done)
        self.progress_.connect(lambda f: self.bar.setValue(int(f * 1000)))

    def refresh(self) -> None:
        """Measure the window's current file / selection (or show the cached result)."""
        import threading
        from . import analysis, media
        self.cancel()
        self.run += 1
        self._idle()
        d = self.win.doc
        if d is None or d.frames <= 0:
            self.key = None
            self.what.setText(t("stats_nothing"))
            self._fill(None)
            return
        a, b = d.sel if d.sel else (0, d.frames)
        self.key = self.win.stats_key(d, a, b)
        rng = t("stats_sel", fmt_time(a, d.rate), fmt_time(b, d.rate), fmt_time(b - a, d.rate)) if d.sel else \
            t("stats_whole", fmt_time(d.frames, d.rate))
        self.what.setText(f"{d.name} · {rng}")
        st = self.win.stats_cache.get(self.key)
        if st is not None and st.has_true_peak:
            self._fill(st)
            return
        self._fill(None, d.labels)
        self.prog = media.Progress(lambda f: self.progress_.emit(f))
        self.run += 1
        prog, key, run = self.prog, self.key, self.run
        self.bar.setValue(0)
        self.bar.show()
        self.cancel_b.show()
        self.refresh_b.setEnabled(False)

        def work():
            try:
                self.finished_.emit(run, key, analysis.analyze(d, a, b, prog), "")
            except media.Cancelled:
                self.finished_.emit(run, key, None, "cancel")
            except Exception as e:                          # the file was closed meanwhile, …
                self.finished_.emit(run, key, None, str(e) or type(e).__name__)
        threading.Thread(target=work, daemon=True).start()

    def _idle(self) -> None:
        self.bar.hide()
        self.cancel_b.hide()
        self.refresh_b.setEnabled(True)

    def cancel(self) -> None:
        if self.prog is not None:
            self.prog.cancel()
            self.prog = None

    def _done(self, run, key, st, err: str) -> None:
        if st is not None:
            self.win.stats_store(key, st)
        if run != self.run:
            return
        self.prog = None
        self._idle()
        if st is not None:
            self._fill(st)
        elif err and err != "cancel":
            self.what.setText(t("op_failed", err))

    def _fill(self, st, labels=None) -> None:
        from PySide6.QtWidgets import QTableWidgetItem
        from . import analysis
        chans = st.channels if st is not None else []
        labels = [c.label for c in chans] if st is not None else list(labels or [])
        cols = labels + ([t("stats_all")] if len(labels) > 1 else [])
        self.table.setColumnCount(max(1, len(cols)))
        self.table.setHorizontalHeaderLabels(cols or [""])
        from PySide6.QtWidgets import QHeaderView
        hh = self.table.horizontalHeader()
        for c in range(self.table.columnCount()):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents if c == len(labels) else QHeaderView.Stretch)
        db = analysis.to_db
        for c in range(self.table.columnCount()):
            if st is None:
                vals = ["…" if labels else "—"] * len(self.ROWS)
            else:
                if c < len(chans):
                    x = chans[c]
                    pk, tp, mn, mx, rms, dc, cl = x.peak, x.true_peak, x.min, x.max, x.rms, x.dc, x.clipped
                else:
                    pk, tp, rms, cl = st.peak, st.true_peak, st.rms, st.clipped
                    mn = min(x.min for x in chans)
                    mx = max(x.max for x in chans)
                    dc = None
                vals = [fmt_db(db(pk), 2), fmt_db(db(tp), 2), fmt_db(mn, 4), fmt_db(mx, 4), fmt_db(db(rms), 2),
                        "—" if dc is None else fmt_db(dc * 100, 3), f"{cl:,}".replace(",", " ")]
            for r, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if r == 6 and st is not None and v != "0":
                    it.setForeground(style.qcolor(style.W_CLIP))
                self.table.setItem(r, c, it)
        self.table.clearSelection()
        if st is None:
            for lab in self.loud.values():
                lab.setText("…" if labels else "—")
            return
        self.loud["st_integrated"].setText(f"{fmt_db(st.integrated)} LUFS")
        self.loud["st_lra"].setText(f"{fmt_db(st.lra)} LU")
        self.loud["st_momentary"].setText(f"{fmt_db(st.momentary_max)} LUFS")
        self.loud["st_short"].setText(f"{fmt_db(st.short_term_max)} LUFS")
        self.loud["st_threshold"].setText(f"{fmt_db(st.threshold)} LUFS")

    def values(self) -> dict:
        """The table as text (tests): {(row key, column header): text} + the loudness rows."""
        out = {}
        for c in range(self.table.columnCount()):
            h = self.table.horizontalHeaderItem(c).text()
            for r, k in enumerate(self.ROWS):
                it = self.table.item(r, c)
                out[(k, h)] = it.text() if it else ""
        for k, lab in self.loud.items():
            out[k] = lab.text()
        return out

    def closeEvent(self, ev) -> None:
        self.cancel()
        super().closeEvent(ev)


class RecoveryDialog(QDialog):
    """At the start after a crash: the documents with unsaved changes, each with a check box."""

    def __init__(self, entries, parent=None):
        import datetime
        super().__init__(parent)
        self.entries = list(entries)
        self.setWindowTitle(t("rc_title"))
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("rc_title")))
        lay.addWidget(_hint(t("rc_note")))
        self.list = QListWidget()
        for e in self.entries:
            when = datetime.datetime.fromtimestamp(e.time).strftime("%Y-%m-%d %H:%M") if e.time else ""
            it = QListWidgetItem(style.icon("fa6s.wave-square"),
                                 t("rc_item", e.name, fmt_time(e.duration * 1000, 1000, False), e.steps, when))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            it.setToolTip(e.data.get("path") or "")
            self.list.addItem(it)
        lay.addWidget(self.list, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = bb.button(QDialogButtonBox.Ok)
        ok.setObjectName("primary")
        ok.setText(t("rc_restore"))
        bb.button(QDialogButtonBox.Cancel).setText(t("rc_discard"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self._discard)
        lay.addWidget(bb)

    def _discard(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        from . import APP_TITLE
        if QMessageBox.question(self, APP_TITLE, t("rc_discard_q")) == QMessageBox.Yes:
            self.reject()

    def chosen(self) -> list:
        return [e for i, e in enumerate(self.entries) if self.list.item(i).checkState() == Qt.Checked]
