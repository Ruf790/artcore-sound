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
