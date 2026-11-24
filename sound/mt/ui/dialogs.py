"""The multitrack's dialogs (3.0): a new project, the mixdown, a track's effect chain, a track's input, the
recording options."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton,
                               QRadioButton, QSpinBox, QVBoxLayout, QWidget)

from ... import media, style
from ...doc import fmt_time
from ...i18n import t
from ..model import Slot

RATES = (44100, 48000, 88200, 96000, 192000)


def default_folder(cfg: dict) -> str:
    d = cfg.get("mt_dir") or ""
    if d and os.path.isdir(d):
        return d
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    return os.path.join(docs if os.path.isdir(docs) else os.path.expanduser("~"), "ArtCore Sound", "Projects")


def safe(name: str) -> str:
    bad = '<>:"/\\|?*'
    return "".join("_" if c in bad or ord(c) < 32 else c for c in name).strip(" .")


class NewProjectDialog(QDialog):
    """Name, folder (the project gets a folder of its own there), rate, the master's channels, tracks."""

    def __init__(self, cfg: dict, parent=None, name: str = ""):
        super().__init__(parent)
        from ...dialogs import _buttons, _head, _hint
        self.cfg = cfg
        self.setWindowTitle(t("mt_new_title"))
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("mt_new_title")))
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        self.name = QLineEdit(name or t("mt_new_name"))
        form.addRow(t("mt_name"), self.name)
        row = QHBoxLayout()
        self.folder = QLineEdit(default_folder(cfg))
        row.addWidget(self.folder, 1)
        b = QPushButton(t("sv_browse"))
        b.clicked.connect(self._browse)
        row.addWidget(b)
        form.addRow(t("mt_folder"), row)
        self.rate = QComboBox()
        for r in RATES:
            self.rate.addItem(f"{r:,} Hz".replace(",", " "), r)
        self.rate.setCurrentIndex(max(0, self.rate.findData(int(cfg.get("mt_rate") or 48000))))
        form.addRow(t("mt_rate"), self.rate)
        self.chans = QComboBox()
        self.chans.addItem(t("ch_stereo"), 2)
        self.chans.addItem("5.1", 6)
        self.chans.setCurrentIndex(max(0, self.chans.findData(int(cfg.get("mt_channels") or 2))))
        form.addRow(t("mt_master_ch"), self.chans)
        self.tracks = QSpinBox()
        self.tracks.setRange(0, 64)
        self.tracks.setValue(int(cfg.get("mt_tracks", 4)))
        form.addRow(t("mt_tracks_n"), self.tracks)
        lay.addLayout(form)
        self.where = _hint("")
        lay.addWidget(self.where)
        lay.addWidget(_hint(t("mt_new_note")))
        lay.addWidget(_buttons(self, t("mt_create")))
        self.name.textChanged.connect(self._show)
        self.folder.textChanged.connect(self._show)
        self._show()

    def _browse(self) -> None:
        p = QFileDialog.getExistingDirectory(self, t("mt_folder"), self.folder.text())
        if p:
            self.folder.setText(os.path.normpath(p))

    def project_path(self) -> str:
        n = safe(self.name.text()) or "Project"
        return os.path.join(self.folder.text().strip(), n, n + ".acproj")

    def _show(self) -> None:
        self.where.setText(t("mt_new_where", self.project_path()))

    def accept(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        from ... import APP_TITLE
        p = self.project_path()
        if os.path.exists(p):
            QMessageBox.warning(self, APP_TITLE, t("mt_exists", p))
            return
        self.cfg["mt_dir"] = self.folder.text().strip()
        self.cfg["mt_rate"] = self.rate.currentData()
        self.cfg["mt_channels"] = self.chans.currentData()
        self.cfg["mt_tracks"] = self.tracks.value()
        super().accept()


class MixdownDialog(QDialog):
    """What (the whole project, the selection, each region, stems, into a new tab), where, the format."""

    def __init__(self, project, cfg: dict, parent=None):
        super().__init__(parent)
        from ...dialogs import _buttons, _head, _hint, option_text
        self._option_text = option_text
        self.p, self.cfg = project, cfg
        self.setWindowTitle(t("mt_mix_title"))
        self.setMinimumWidth(660)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("mt_mix_title")))
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        self.what = {}
        sel = project.sel
        regs = project.regions()
        for k, text, on in (("whole", t("mt_mix_whole", fmt_time(project.frames, project.rate)), True),
                            ("sel", t("mt_mix_sel", fmt_time(sel[1] - sel[0], project.rate)) if sel else
                             t("mt_mix_sel_none"), bool(sel)),
                            ("regions", t("mt_mix_regions", len(regs)), bool(regs)),
                            ("stems", t("mt_mix_stems"), bool(project.audio_tracks())),
                            ("tab", t("mt_mix_tab"), True)):
            r = QRadioButton(text)
            r.setEnabled(on)
            self.what[k] = r
            v.addWidget(r)
        last = cfg.get("mt_mix_what") or "whole"
        (self.what.get(last) if self.what.get(last) and self.what[last].isEnabled() else self.what["whole"]) \
            .setChecked(True)
        lay.addWidget(box)
        self.stem_list = QListWidget()
        self.stem_list.setMaximumHeight(120)
        for tr in project.audio_tracks():
            it = QListWidgetItem(tr.name)
            it.setData(Qt.UserRole, tr.id)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if tr.clips else Qt.Unchecked)
            self.stem_list.addItem(it)
        lay.addWidget(self.stem_list)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        row = QHBoxLayout()
        stem = safe(project.name or "Mix") or "Mix"
        base = cfg.get("mt_mix_dir") or (project.folder or os.path.expanduser("~"))
        self.path = QLineEdit(os.path.join(base, stem))
        row.addWidget(self.path, 1)
        b = QPushButton(t("sv_browse"))
        b.clicked.connect(self._browse)
        row.addWidget(b)
        self.path_label = QLabel(t("sv_name"))
        form.addRow(self.path_label, row)
        self.fmt = QComboBox()
        for k, f in media.FORMATS.items():
            self.fmt.addItem(f.name, k)
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(cfg.get("mt_mix_format") or "wav")))
        form.addRow(t("sv_format"), self.fmt)
        self.opt = QComboBox()
        form.addRow(t("sv_option"), self.opt)
        self.tail = QCheckBox(t("mt_mix_tail"))
        self.tail.setChecked(bool(cfg.get("mt_mix_tail", True)))
        form.addRow("", self.tail)
        lay.addLayout(form)
        self.note = _hint("")
        lay.addWidget(self.note)
        lay.addWidget(_buttons(self, t("mt_mix_go")))
        self.fmt.currentIndexChanged.connect(lambda _i: self._fill(""))
        for r in self.what.values():
            r.toggled.connect(lambda _on: self._sync())
        self._fill(cfg.get("mt_mix_option") or "")
        self._sync()

    def kind(self) -> str:
        return next(k for k, r in self.what.items() if r.isChecked())

    def _fill(self, option: str) -> None:
        k = self.format()
        self.opt.clear()
        for o in media.FORMATS[k].options:
            self.opt.addItem(self._option_text(k, o), o)
        want = option or media.default_option(k, self.p.channels, 24)
        self.opt.setCurrentIndex(max(0, self.opt.findData(want)))
        self._sync()

    def _sync(self) -> None:
        k = self.kind()
        folder = k in ("regions", "stems")
        self.path_label.setText(t("rg_folder") if folder else t("sv_name"))
        self.stem_list.setVisible(k == "stems")
        for w in (self.path, self.fmt, self.opt):
            w.setEnabled(k != "tab")
        notes = []
        ch, rate = media.out_shape(self.format(), self.p.channels, self.p.rate)
        if ch < self.p.channels:
            notes.append(t("sv_mp3_stereo") if self.format() == "mp3" else t("sv_ac3_51"))
        if rate != self.p.rate:
            notes.append(t("sv_rate", self.p.rate, rate))
        self.note.setText("\n".join(notes))
        self.note.setVisible(bool(notes))

    def _browse(self) -> None:
        if self.kind() in ("regions", "stems"):
            p = QFileDialog.getExistingDirectory(self, t("rg_folder"), os.path.dirname(self.path.text()))
            if p:
                self.path.setText(os.path.normpath(p))
            return
        f = media.FORMATS[self.format()]
        p, _ = QFileDialog.getSaveFileName(self, t("mt_mix_title"), self.path.text(), f"{f.name} (*{f.ext})")
        if p:
            self.path.setText(os.path.normpath(p))

    def format(self) -> str:
        return self.fmt.currentData() or "wav"

    def option(self) -> str:
        return self.opt.currentData() or media.FORMATS[self.format()].default

    def target(self) -> str:
        p = self.path.text().strip()
        if self.kind() in ("whole", "sel"):
            ext = media.FORMATS[self.format()].ext
            if p and not p.lower().endswith(ext):
                p = os.path.splitext(p)[0] + ext if os.path.splitext(p)[1].lower() in media.EXT_FORMAT else p + ext
        return p

    def stem_ids(self) -> list[int]:
        return [self.stem_list.item(i).data(Qt.UserRole) for i in range(self.stem_list.count())
                if self.stem_list.item(i).checkState() == Qt.Checked]

    def accept(self) -> None:
        self.cfg["mt_mix_what"] = self.kind()
        self.cfg["mt_mix_format"] = self.format()
        self.cfg["mt_mix_option"] = self.option()
        self.cfg["mt_mix_tail"] = self.tail.isChecked()
        p = self.target()
        if p:
            self.cfg["mt_mix_dir"] = p if self.kind() in ("regions", "stems") else os.path.dirname(p)
        super().accept()


class ChainDialog(QDialog):
    """A track's (or the master's) effects in order: add, remove, move, bypass; a double click (or Edit) opens an
    effect's own window — its changes are heard at once while the project plays."""

    def __init__(self, win, project, tid: int, parent=None):
        super().__init__(parent or win)
        from ...dialogs import _head, _hint
        self.win, self.p, self.tid = win, project, tid
        tr = project.track(tid)
        self.setWindowTitle(t("mt_chain_title", tr.name if tr.kind != "master" else t("mt_master")))
        self.setMinimumSize(520, 420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(self.windowTitle()))
        lay.addWidget(_hint(t("mt_chain_note")))
        row = QHBoxLayout()
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _it: self.edit())
        self.list.itemChanged.connect(self._toggled)
        row.addWidget(self.list, 1)
        col = QVBoxLayout()
        self.b_add = QPushButton(style.icon("fa6s.plus"), t("mt_chain_add"))
        self.add_menu = QMenu(self)
        self.add_menu.aboutToShow.connect(self._fill_add)
        self.b_add.setMenu(self.add_menu)
        col.addWidget(self.b_add)
        for icon, text, slot in (("fa6s.pen", t("mt_chain_edit"), self.edit),
                                 ("fa6s.arrow-up", t("mt_chain_up"), lambda: self.move(-1)),
                                 ("fa6s.arrow-down", t("mt_chain_down"), lambda: self.move(1)),
                                 ("fa6s.trash-can", t("mt_chain_remove"), self.remove)):
            b = QPushButton(style.icon(icon), text)
            b.clicked.connect(slot)
            col.addWidget(b)
        col.addStretch(1)
        row.addLayout(col)
        lay.addLayout(row, 1)
        self.errors = _hint("")
        self.errors.setStyleSheet("color: #e5534b;")
        lay.addWidget(self.errors)
        close = QPushButton(t("close"))
        close.clicked.connect(self.accept)
        h = QHBoxLayout()
        h.addStretch(1)
        h.addWidget(close)
        lay.addLayout(h)
        self._lock = False
        self.fill()

    def slots(self):
        tr = self.p.track(self.tid)
        return tr.fx if tr is not None else ()

    def fill(self, select: int | None = None) -> None:
        from .mixer import slot_name
        self._lock = True
        self.list.clear()
        eng = self.win.mt_engine(self.p)
        errs = eng.errors if eng is not None else {}
        bad = []
        for s in self.slots():
            it = QListWidgetItem(slot_name(s))
            it.setData(Qt.UserRole, s.id)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked if s.bypass else Qt.Checked)
            if s.id in errs:
                it.setForeground(style.qcolor("#e5534b"))
                bad.append(f"{slot_name(s)}: {errs[s.id]}")
            self.list.addItem(it)
            if s.id == select:
                self.list.setCurrentItem(it)
        self.errors.setText("\n".join(bad))
        self.errors.setVisible(bool(bad))
        self._lock = False

    def _set(self, slots, label: str) -> None:
        self.p.set_fx(self.tid, tuple(slots), label)
        self.win.mt_edited(self.p)

    def _fill_add(self) -> None:
        from ... import fx
        from ..engine import track_effects
        m = self.add_menu
        m.clear()
        for k in track_effects():
            a = m.addAction(t(fx.EFFECTS[k].name))
            a.triggered.connect(lambda _c=False, k=k: self.add(k))
        try:
            from ...vst import scan
            plugins = scan.library(self.win.cfg).plugins()
        except Exception:                               # noqa: BLE001
            plugins = []
        sub = m.addMenu(style.icon("fa6s.plug"), t("vst_menu"))
        if not plugins:
            a = sub.addAction(t("mt_no_plugins"))
            a.setEnabled(False)
        for pl in plugins:
            a = sub.addAction(pl.name + (f" — {pl.maker}" if pl.maker else ""))
            a.triggered.connect(lambda _c=False, pl=pl: self.add("vst3", {"plugin": pl.ref()}))

    def add(self, key: str, values: dict | None = None) -> None:
        from ... import fx
        eff = fx.get(key)
        last = (self.win.cfg.get("fx_last") or {}).get(key) or {} if key != "vst3" else {}
        vals = eff.clean({**eff.defaults(), **last, **(values or {})})
        s = Slot(key, vals)
        self._set(self.slots() + (s,), t("mt_u_fx_add"))
        new = self.slots()[-1]
        self.fill(new.id)
        self.edit()

    def current(self):
        it = self.list.currentItem()
        if it is None:
            return None
        sid = it.data(Qt.UserRole)
        return next((s for s in self.slots() if s.id == sid), None)

    def edit(self) -> None:
        s = self.current()
        if s is not None:
            self.win.mt_edit_slot(self.p, self.tid, s.id, self)
            self.fill(s.id)

    def move(self, d: int) -> None:
        s = self.current()
        if s is None:
            return
        slots = list(self.slots())
        i = slots.index(s)
        j = max(0, min(len(slots) - 1, i + d))
        if i != j:
            slots.insert(j, slots.pop(i))
            self._set(slots, t("mt_u_fx_move"))
            self.fill(s.id)

    def remove(self) -> None:
        s = self.current()
        if s is not None:
            self._set([x for x in self.slots() if x.id != s.id], t("mt_u_fx_remove"))
            self.fill()

    def _toggled(self, it) -> None:
        if self._lock:
            return
        from dataclasses import replace
        sid = it.data(Qt.UserRole)
        off = it.checkState() != Qt.Checked
        self._set([replace(x, bypass=off) if x.id == sid else x for x in self.slots()], t("mt_u_fx"))


class InputDialog(QDialog):
    """The input a track records from: the device and its channels."""

    def __init__(self, tr, parent=None):
        super().__init__(parent)
        from ...dialogs import _buttons, _head, _hint
        from ... import record
        self.tr = tr
        self.setWindowTitle(t("mt_input_title", tr.name))
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(self.windowTitle()))
        form = QFormLayout()
        self.dev = QComboBox()
        self.devs = record.input_devices()
        self.dev.addItem(t("mt_input_default"), "")
        for d in self.devs:
            self.dev.addItem(f"{d['name']} ({d['channels']})", d["name"])
        self.dev.setCurrentIndex(max(0, self.dev.findData(tr.input)))
        form.addRow(t("rec_device"), self.dev)
        self.ch = QComboBox()
        form.addRow(t("mt_input_ch"), self.ch)
        lay.addLayout(form)
        lay.addWidget(_hint(t("mt_input_note")))
        lay.addWidget(_buttons(self))
        self.dev.currentIndexChanged.connect(lambda _i: self._fill())
        self._fill()

    def _fill(self) -> None:
        name = self.dev.currentData()
        d = next((x for x in self.devs if x["name"] == name), None) or \
            next((x for x in self.devs if x.get("default")), self.devs[0] if self.devs else None)
        n = int(d["channels"]) if d else 2
        self.ch.clear()
        if self.tr.channels == 1 or n == 1:
            for c in range(n):
                self.ch.addItem(t("mt_input_one", c + 1), (c,))
        if self.tr.channels >= 2 and n >= 2:
            for c in range(0, n - 1, 2):
                self.ch.addItem(t("mt_input_pair", c + 1, c + 2), (c, c + 1))
        if self.tr.channels == 6 and n >= 6:
            self.ch.addItem(t("mt_input_six"), tuple(range(6)))
        cur = tuple(self.tr.input_ch)
        self.ch.setCurrentIndex(max(0, self.ch.findData(cur)) if cur else 0)

    def values(self) -> dict:
        return {"input": self.dev.currentData() or "", "input_ch": tuple(self.ch.currentData() or ())}


class RecOptionsDialog(QDialog):
    """The pre-roll before a punch-in, the input's offset (what the clocks do not see: converters, a cable)."""

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        from ...dialogs import _buttons, _head, _hint
        self.cfg = cfg
        self.setWindowTitle(t("mt_recopt_title"))
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("mt_recopt_title")))
        form = QFormLayout()
        self.preroll = QDoubleSpinBox()
        self.preroll.setRange(0, 20)
        self.preroll.setDecimals(1)
        self.preroll.setSuffix(" " + t("un_s"))
        self.preroll.setValue(float(cfg.get("mt_preroll", 2.0)))
        form.addRow(t("mt_preroll"), self.preroll)
        self.offset = QDoubleSpinBox()
        self.offset.setRange(-500, 500)
        self.offset.setDecimals(1)
        self.offset.setSuffix(" " + t("un_ms"))
        self.offset.setValue(float(cfg.get("mt_rec_offset_ms", 0.0)))
        form.addRow(t("mt_rec_offset"), self.offset)
        lay.addLayout(form)
        lay.addWidget(_hint(t("mt_rec_offset_note")))
        lay.addWidget(_buttons(self))

    def accept(self) -> None:
        self.cfg["mt_preroll"] = self.preroll.value()
        self.cfg["mt_rec_offset_ms"] = self.offset.value()
        super().accept()
