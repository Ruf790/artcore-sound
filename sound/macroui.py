"""Macros in the window (2.0): the window of one macro (its name, hotkey and steps — reorder, switch off, change,
delete) shown when a recording stops and from «Manage macros», and the list of the macros (run on the file or its
selection, change, duplicate, delete, import and export .acmacro files)."""
from __future__ import annotations

import copy
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
                               QKeySequenceEdit, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QVBoxLayout)

from . import APP_TITLE, batch, macro, style
from .i18n import t

ICONS = {"select": "fa6s.i-cursor", "select_none": "fa6s.expand", "cut": "fa6s.scissors", "copy": "fa6s.copy",
         "paste": "fa6s.paste", "delete": "fa6s.eraser", "silence": "fa6s.volume-off",
         "insert_silence": "fa6s.arrows-left-right-to-line", "trim": "fa6s.crop-simple", "save": "fa6s.floppy-disk",
         "fade_in": "fa6s.arrow-trend-up", "fade_out": "fa6s.arrow-trend-down", "reverse": "fa6s.right-left",
         "invert": "fa6s.arrow-down-up-across-line", "to_mono": "fa6s.compress", "to_stereo": "fa6s.expand"}


def step_icon(kind: str) -> str:
    from .batchui import STEP_ICONS
    return ICONS.get(kind) or STEP_ICONS.get(kind) or "fa6s.circle"


def _head(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet("font-size: 13pt; font-weight: 700; padding-bottom: 2px;")
    return lab


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("hint")
    lab.setWordWrap(True)
    return lab


# ── one step ──────────────────────────────────────────────────
class StepDialog(QDialog):
    """The parameters of a macro's own step (a selection, silence to insert, a fade's curve, how to save)."""

    def __init__(self, step: macro.Step, parent=None):
        from .fxui import ParamRow
        super().__init__(parent)
        self.step = step
        self.values = dict(step.values)
        self.setWindowTitle(step.title())
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(step.title()))
        note = {"select": "mc_note_select", "save": "mc_note_save"}.get(step.kind)
        if note:
            lay.addWidget(_hint(t(note)))
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        self.rows: dict = {}
        if step.kind == "save":
            from . import media
            self.fmt = QComboBox()
            for k in media.FORMATS:
                self.fmt.addItem(k.upper(), k)
            self.fmt.setCurrentIndex(max(0, self.fmt.findData(self.values["format"])))
            form.addRow(t("sv_format"), self.fmt)
            self.opt = QLineEdit(self.values["option"])
            self.opt.setPlaceholderText(t("mc_option_default"))
            form.addRow(t("sv_option"), self.opt)
            self.tmpl = QLineEdit(self.values["template"])
            self.tmpl.setToolTip(t("mc_template_tip"))
            form.addRow(t("bt_template"), self.tmpl)
            row = QHBoxLayout()
            self.folder = QLineEdit(self.values["folder"])
            self.folder.setPlaceholderText(t("mc_folder_same"))
            row.addWidget(self.folder, 1)
            b = QPushButton(t("sv_browse"))
            b.clicked.connect(self._browse)
            row.addWidget(b)
            form.addRow(t("mc_folder"), row)
        else:
            for p in macro.step_params(step.kind):
                r = ParamRow(p, self.values[p.key], self)
                r.changed.connect(lambda k, v: self.values.__setitem__(k, v))
                self.rows[p.key] = r
                form.addRow("" if p.kind == "bool" else t(p.label), r)
        lay.addLayout(form)
        from .dialogs import _buttons
        lay.addWidget(_buttons(self))

    def _browse(self) -> None:
        p = QFileDialog.getExistingDirectory(self, t("mc_folder"), self.folder.text())
        if p:
            self.folder.setText(os.path.normpath(p))

    def result_values(self) -> dict:
        if self.step.kind == "save":
            return {"format": self.fmt.currentData(), "option": self.opt.text().strip(),
                    "template": self.tmpl.text().strip() or "{name}", "folder": self.folder.text().strip()}
        return dict(self.values)


def edit_step(win, parent, s: macro.Step) -> dict | None:
    """The window that changes a step (the effect's own for an effect) → its new values, None: cancelled."""
    if s.kind in batch.FX_STEPS:
        from . import fxui
        from .doc import new_document
        d = new_document(win.session.folder(), 48000, 2)
        try:
            dlg = fxui.EffectDialog(win, s.kind, d, 0, 0, True, {"values": s.values, "file": ""})
            dlg.setParent(parent, dlg.windowFlags())
            ok = dlg.exec()
            return dlg.result_values() if ok else None
        finally:
            d.close()
    if s.kind in batch.STEP_KINDS:
        from .batchui import StepDialog as BatchStep
        if not batch.step_params(s.kind):
            return None
        dlg = BatchStep(batch.Step(s.kind, s.values), parent)
        return dlg.result_values() if dlg.exec() else None
    if not macro.step_params(s.kind) and s.kind != "save":
        return None
    dlg = StepDialog(s, parent)
    return dlg.result_values() if dlg.exec() else None


# ── one macro ─────────────────────────────────────────────────
class MacroDialog(QDialog):
    """A macro's name, hotkey and steps. new=True: right after recording (OK saves it)."""

    def __init__(self, win, m: macro.Macro, new: bool = False, embedded: bool = False):
        """embedded: a batch's copy of a macro — no hotkey, not saved as a macro."""
        super().__init__(win)
        self.win = win
        self.embedded = embedded
        self.m = copy.deepcopy(m)
        self.setWindowTitle(t("mc_title"))
        self.setMinimumSize(640, 520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("mc_new") if new else t("mc_title")))
        form = QFormLayout()
        self.name = QLineEdit(m.name or self._free_name())
        form.addRow(t("mc_name"), self.name)
        row = QHBoxLayout()
        self.key = QKeySequenceEdit(QKeySequence(m.hotkey))
        self.key.setMaximumSequenceLength(1)
        self.key.keySequenceChanged.connect(lambda _k: self._check_key())
        row.addWidget(self.key, 1)
        b = QPushButton(t("mc_key_clear"))
        b.clicked.connect(self.key.clear)
        row.addWidget(b)
        if not embedded:
            form.addRow(t("mc_key"), row)
        else:
            self.key.hide()
            b.hide()
        lay.addLayout(form)
        self.key_note = _hint("")
        lay.addWidget(self.key_note)
        lay.addWidget(QLabel(t("mc_steps")))
        row = QHBoxLayout()
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.itemDoubleClicked.connect(lambda _it: self._edit())
        row.addWidget(self.list, 1)
        col = QVBoxLayout()
        for text, icon, slot in ((t("bt_edit_step") + "…", "fa6s.pen", self._edit),
                                 (t("bt_del_step"), "fa6s.trash-can", self._delete),
                                 (t("bt_up"), "fa6s.arrow-up", lambda: self._move(-1)),
                                 (t("bt_down"), "fa6s.arrow-down", lambda: self._move(1))):
            b = QPushButton(style.icon(icon), text)
            b.clicked.connect(slot)
            col.addWidget(b)
        col.addStretch(1)
        row.addLayout(col)
        lay.addLayout(row, 1)
        lay.addWidget(_hint(t("mc_steps_note")))
        for s in self.m.steps:
            self.list.addItem(self._item(s))
        from .dialogs import _buttons
        lay.addWidget(_buttons(self))
        self._check_key()

    @staticmethod
    def _free_name() -> str:
        names = {m.name for m in macro.load_all()}
        i = 1
        while t("mc_default_name", i) in names:
            i += 1
        return t("mc_default_name", i)

    def _item(self, s: macro.Step) -> QListWidgetItem:
        it = QListWidgetItem(style.icon(step_icon(s.kind)), "")
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
        it.setCheckState(Qt.Checked if s.enabled else Qt.Unchecked)
        it.setData(Qt.UserRole, s)
        self._label(it)
        return it

    @staticmethod
    def _label(it) -> None:
        s = it.data(Qt.UserRole)
        summ = s.summary()
        it.setText(s.title() + (f"  —  {summ}" if summ else ""))

    def _edit(self) -> None:
        it = self.list.currentItem()
        if it is None:
            return
        s = it.data(Qt.UserRole)
        v = edit_step(self.win, self, s)
        if v is not None:
            it.setData(Qt.UserRole, macro.Step(s.kind, v, it.checkState() == Qt.Checked))
            self._label(it)

    def _delete(self) -> None:
        r = self.list.currentRow()
        if r >= 0:
            self.list.takeItem(r)

    def _move(self, k: int) -> None:
        r = self.list.currentRow()
        if r < 0 or not 0 <= r + k < self.list.count():
            return
        it = self.list.takeItem(r)
        self.list.insertItem(r + k, it)
        self.list.setCurrentRow(r + k)

    def hotkey(self) -> str:
        return self.key.keySequence().toString()

    def _check_key(self) -> bool:
        k = self.hotkey()
        clash = ""
        if k:
            if k in self.win._taken_keys():
                clash = t("mc_key_taken", k)
            else:
                for m in macro.load_all():
                    if m.hotkey == k and os.path.normcase(m.path or "") != os.path.normcase(self.m.path or ""):
                        clash = t("mc_key_macro", k, m.name)
        self.key_note.setText(clash)
        return not clash

    def result(self) -> macro.Macro:
        steps = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            s = it.data(Qt.UserRole)
            steps.append(macro.Step(s.kind, dict(s.values), it.checkState() == Qt.Checked))
        return macro.Macro(self.name.text().strip(), steps, self.hotkey(), self.m.path)

    def accept(self) -> None:
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, APP_TITLE, t("mc_need_name"))
            return
        if self.embedded:
            super().accept()
            return
        if not self._check_key():
            QMessageBox.warning(self, APP_TITLE, self.key_note.text())
            return
        other = macro.find(name)
        if other is not None and os.path.normcase(other.path) != os.path.normcase(self.m.path or "") and \
                QMessageBox.question(self, APP_TITLE, t("mc_replace", name)) != QMessageBox.Yes:
            return
        super().accept()


# ── the list ──────────────────────────────────────────────────
class MacroManager(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle(t("mc_manage"))
        self.setMinimumSize(560, 420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("mc_manage")))
        row = QHBoxLayout()
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _it: self._edit())
        row.addWidget(self.list, 1)
        col = QVBoxLayout()
        self.b_run = QPushButton(style.icon("fa6s.play"), t("mc_run"))
        self.b_run.clicked.connect(self._run)
        col.addWidget(self.b_run)
        for text, icon, slot in ((t("bt_edit_step") + "…", "fa6s.pen", self._edit),
                                 (t("mc_duplicate"), "fa6s.clone", self._duplicate),
                                 (t("bt_del_step"), "fa6s.trash-can", self._delete),
                                 (t("mc_import") + "…", "fa6s.folder-open", self._import),
                                 (t("mc_export") + "…", "fa6s.floppy-disk", self._export)):
            b = QPushButton(style.icon(icon), text)
            b.clicked.connect(slot)
            col.addWidget(b)
        col.addStretch(1)
        row.addLayout(col)
        lay.addLayout(row, 1)
        lay.addWidget(_hint(t("mc_manage_note")))
        b = QPushButton(t("close"))
        b.clicked.connect(self.accept)
        h = QHBoxLayout()
        h.addStretch(1)
        h.addWidget(b)
        lay.addLayout(h)
        self.fill()

    def fill(self, select: str = "") -> None:
        self.list.clear()
        for m in macro.load_all():
            text = m.name + (f"   [{m.hotkey}]" if m.hotkey else "") + f"   —   {t('mc_n_steps', len(m.steps))}"
            it = QListWidgetItem(style.icon("fa6s.play"), text)
            it.setData(Qt.UserRole, m)
            self.list.addItem(it)
            if m.name == select:
                self.list.setCurrentItem(it)
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)
        self.b_run.setEnabled(self.win.doc is not None and self.list.count() > 0)

    def current(self) -> macro.Macro | None:
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it is not None else None

    def _run(self) -> None:
        m = self.current()
        if m is not None and self.win.doc is not None:
            self.accept()
            self.win.run_macro(m)

    def _edit(self) -> None:
        m = self.current()
        if m is None:
            return
        dlg = MacroDialog(self.win, m)
        if dlg.exec():
            new = dlg.result()
            macro.save(new)
            self.fill(new.name)

    def _duplicate(self) -> None:
        m = self.current()
        if m is None:
            return
        names = {x.name for x in macro.load_all()}
        name = t("mc_copy_name", m.name)
        i = 2
        while name in names:
            name = t("mc_copy_name", m.name) + f" {i}"
            i += 1
        macro.save(macro.Macro(name, copy.deepcopy(m.steps), ""), "")
        self.fill(name)

    def _delete(self) -> None:
        m = self.current()
        if m is not None and QMessageBox.question(self, APP_TITLE, t("mc_delete_q", m.name)) == QMessageBox.Yes:
            macro.delete(m)
            self.fill()

    def _import(self) -> None:
        path, _f = QFileDialog.getOpenFileName(self, t("mc_import"), "", t("mc_filter") + f" (*{macro.EXT})")
        if not path:
            return
        try:
            m = macro.load(path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, APP_TITLE, t("mc_bad_file", os.path.basename(path), str(e)))
            return
        m.path = ""
        if m.hotkey and m.hotkey in self.win._taken_keys():
            m.hotkey = ""
        macro.save(m)
        self.fill(m.name)

    def _export(self) -> None:
        m = self.current()
        if m is None:
            return
        path, _f = QFileDialog.getSaveFileName(self, t("mc_export"), macro.file_name(m.name),
                                               t("mc_filter") + f" (*{macro.EXT})")
        if path:
            import json
            with open(path, "w", encoding="utf-8") as f:
                json.dump(m.to_dict(), f, ensure_ascii=False, indent=1)
