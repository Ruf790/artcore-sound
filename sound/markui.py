"""The list of markers and regions on the right of the window (0.4): name, start, end, length, sorted by time.
A double click goes to a marker / selects a region; F2 or a click on a selected name renames; Del deletes. And the
window of «Save regions as separate files»."""
from __future__ import annotations

import os

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from . import media, style
from .doc import fmt_time
from .i18n import t


class MarkerList(QTreeWidget):
    activated = Signal(int)              # a double click: go to the marker / select the region
    renamed = Signal(int, str)
    deleteRequested = Signal(list)       # ids

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("markers")
        self.setColumnCount(4)
        self.setHeaderLabels([t("mk_col_name"), t("mk_col_start"), t("mk_col_end"), t("mk_col_len")])
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditKeyPressed | QAbstractItemView.SelectedClicked)
        self.setSortingEnabled(False)
        hh = self.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.doc = None
        self._key = None
        self._filling = False
        self.itemDoubleClicked.connect(self._double)
        self.itemChanged.connect(self._changed)

    def fill(self, d, force: bool = False) -> None:
        key = (id(d), d.mversion if d is not None else 0, d.rate if d is not None else 0)
        if key == self._key and not force:
            return
        self._key = key
        self.doc = d
        sel = set(self.selected_ids())
        self._filling = True
        self.clear()
        if d is not None:
            for m in d.markers:
                reg = m.end is not None
                it = QTreeWidgetItem([m.name, fmt_time(m.pos, d.rate), fmt_time(m.end, d.rate) if reg else "",
                                      fmt_time(m.length, d.rate) if reg else ""])
                it.setData(0, Qt.UserRole, m.id)
                it.setIcon(0, style.icon("fa6s.flag" if not reg else "fa6s.arrows-left-right-to-line",
                                         style.W_MARKER))
                it.setFlags(it.flags() | Qt.ItemIsEditable)
                for c in (1, 2, 3):
                    it.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
                self.addTopLevelItem(it)
                if m.id in sel:
                    it.setSelected(True)
        self._filling = False

    def selected_ids(self) -> list[int]:
        return [int(it.data(0, Qt.UserRole)) for it in self.selectedItems()]

    def select_id(self, mid: int) -> None:
        for i in range(self.topLevelItemCount()):
            it = self.topLevelItem(i)
            if it.data(0, Qt.UserRole) == mid:
                self.setCurrentItem(it)
                self.scrollToItem(it)
                return

    def rename(self, mid: int) -> None:
        """Start editing the name of this marker in the list."""
        for i in range(self.topLevelItemCount()):
            it = self.topLevelItem(i)
            if it.data(0, Qt.UserRole) == mid:
                self.setCurrentItem(it)
                self.editItem(it, 0)
                return

    def _double(self, it, _col) -> None:
        self.activated.emit(int(it.data(0, Qt.UserRole)))

    def _changed(self, it, col) -> None:
        if self._filling or col != 0:
            return
        self.renamed.emit(int(it.data(0, Qt.UserRole)), it.text(0).strip())

    def event(self, ev) -> bool:
        # Del / F2 / Enter belong to the list while it has the focus (not to the window's Delete = delete sound)
        if ev.type() == QEvent.ShortcutOverride and ev.key() in (Qt.Key_Delete, Qt.Key_F2, Qt.Key_Return,
                                                                  Qt.Key_Enter) and self.state() != \
                QAbstractItemView.EditingState:
            ev.accept()
            return True
        return super().event(ev)

    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key_Delete and self.state() != QAbstractItemView.EditingState:
            ids = self.selected_ids()
            if ids:
                self.deleteRequested.emit(ids)
            return
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and self.state() != QAbstractItemView.EditingState:
            it = self.currentItem()
            if it is not None:
                self.activated.emit(int(it.data(0, Qt.UserRole)))
            return
        super().keyPressEvent(ev)


class RegionsDialog(QDialog):
    """«Save regions as separate files»: the folder, the format and its quality (as in Save as), names."""

    def __init__(self, d, folder: str, fmt: str, option: str, parent=None):
        super().__init__(parent)
        from .dialogs import _buttons, _head, _hint, option_text
        self._option_text = option_text
        self.doc = d
        self.setWindowTitle(t("rg_title"))
        self.setMinimumWidth(600)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("rg_title")))
        n = sum(1 for m in d.markers if m.end is not None)
        lay.addWidget(_hint(t("rg_text", n)))
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        row = QHBoxLayout()
        self.folder = QLineEdit(folder)
        row.addWidget(self.folder, 1)
        b = QPushButton(t("sv_browse"))
        b.clicked.connect(self._browse)
        row.addWidget(b)
        form.addRow(t("rg_folder"), row)
        self.fmt = QComboBox()
        for k, f in media.FORMATS.items():
            self.fmt.addItem(f.name, k)
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(fmt)))
        form.addRow(t("sv_format"), self.fmt)
        self.opt = QComboBox()
        form.addRow(t("sv_option"), self.opt)
        self.numbered = QCheckBox(t("rg_numbered"))
        self.numbered.setChecked(True)
        form.addRow("", self.numbered)
        lay.addLayout(form)
        lay.addWidget(_hint(t("rg_note")))
        lay.addWidget(_buttons(self, t("save")))
        self.fmt.currentIndexChanged.connect(lambda _i: self._fill(""))
        self._fill(option)

    def _fill(self, option: str) -> None:
        k = self.format()
        self.opt.clear()
        for o in media.FORMATS[k].options:
            self.opt.addItem(self._option_text(k, o), o)
        want = option or media.default_option(k, self.doc.channels, getattr(self.doc.track, "bits", 0) or 0)
        self.opt.setCurrentIndex(max(0, self.opt.findData(want)))

    def _browse(self) -> None:
        p = QFileDialog.getExistingDirectory(self, t("rg_folder"), self.folder.text())
        if p:
            self.folder.setText(os.path.normpath(p))

    def format(self) -> str:
        return self.fmt.currentData() or "wav"

    def option(self) -> str:
        return self.opt.currentData() or media.FORMATS[self.format()].default

    def path(self) -> str:
        return self.folder.text().strip()


def region_paths(d, folder: str, fmt: str, numbered: bool = True) -> list[tuple]:
    """(path, start, end) of every region's file: the region's name (cleaned), numbered, never twice the same."""
    from .marks import safe_name
    ext = media.FORMATS[fmt].ext
    out, used = [], set()
    regs = [m for m in d.markers if m.end is not None]
    for i, m in enumerate(regs, 1):
        base = safe_name(m.name, t("mk_region_n", i))
        if numbered:
            base = f"{i:02d} {base}"
        name, k = base, 2
        while name.lower() in used:
            name = f"{base} ({k})"
            k += 1
        used.add(name.lower())
        out.append((os.path.join(folder, name + ext), m.pos, m.end))
    return out
