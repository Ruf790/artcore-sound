"""The batch window (Tools → Batch processing…, 1.0): where the files come from, the chain of steps, how the results
are saved, and the run — the progress of every file and of the whole, Pause, Cancel, the log and the report.
The work itself is batch.py's (worker processes); this window only edits the Job and polls the Runner."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog,
                               QFormLayout, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton,
                               QRadioButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QToolButton, QVBoxLayout, QWidget)

from . import APP_TITLE, AUDIO_EXT, VIDEO_EXT, batch, config, media, style, winint
from .dialogs import _head, _hint, option_text
from .i18n import t

STEP_ICONS = {"gain": "fa6s.volume-high", "normalize": "fa6s.chart-simple", "dc": "fa6s.arrows-up-to-line",
              "trim_silence": "fa6s.down-left-and-up-right-to-center", "fades": "fa6s.arrow-trend-up",
              "channels": "fa6s.code-branch", "rate": "fa6s.gauge", "eq": "fa6s.sliders", "geq": "fa6s.chart-column",
              "comp": "fa6s.compress", "limit": "fa6s.arrows-down-to-line", "delay": "fa6s.clone",
              "reverb": "fa6s.church", "tempo": "fa6s.gauge-high", "nr": "fa6s.wand-magic-sparkles",
              "declick": "fa6s.bolt", "chmatrix": "fa6s.table-cells", "dialog": "fa6s.comment"}
STATUS_COLORS = {"ok": "#3fb27f", "error": "#e5534b", "skip": "#c69026", "cancel": "#8b949e", "run": "#4c9aff"}
COLS = ("n", "file", "status", "progress", "lufs", "peak", "time", "note")


def _fmt_dur(sec: float) -> str:
    sec = int(round(max(0.0, sec)))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _pair(a, b, dec: int = 1) -> str:
    if a is None and b is None:
        return ""
    fa = "—" if a is None else f"{a:.{dec}f}"
    fb = "—" if b is None else f"{b:.{dec}f}"
    return f"{fa} → {fb}"


class StepDialog(QDialog):
    """The parameters of a Process step (volume, normalization, DC, silence, fades, channels, rate) — the same
    rows as the effect windows; normalization has the usual targets as buttons."""

    def __init__(self, step: batch.Step, parent=None):
        from .fxui import ParamRow
        super().__init__(parent)
        self.step = step
        self.values = dict(step.values)
        self.setWindowTitle(step.title())
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(step.title()))
        lay.addWidget(_hint(t("bs_note_" + step.kind)))
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        self.rows = {}
        for p in batch.step_params(step.kind):
            r = ParamRow(p, self.values[p.key], self)
            r.changed.connect(self._changed)
            self.rows[p.key] = r
            form.addRow("" if p.kind == "bool" else t(p.label), r)
        lay.addLayout(form)
        if step.kind == "normalize":
            row = QHBoxLayout()
            row.addWidget(QLabel(t("bs_targets")))
            for lufs, key in ((-23.0, "bs_t_ebu"), (-16.0, "bs_t_podcast"), (-14.0, "bs_t_stream")):
                b = QPushButton(t(key))
                b.clicked.connect(lambda _c=False, v=lufs: self._target(v))
                row.addWidget(b)
            row.addStretch(1)
            lay.addLayout(row)
            self._sync()
        from .dialogs import _buttons
        lay.addWidget(_buttons(self))

    def _target(self, lufs: float) -> None:
        self.rows["mode"].set_value("loudness")
        self.rows["target"].set_value(lufs)
        self.rows["ceiling"].set_value(-1.0)
        self.values.update(mode="loudness", target=lufs, ceiling=-1.0)
        self._sync()

    def _changed(self, key: str, v) -> None:
        self.values[key] = v
        self._sync()

    def _sync(self) -> None:
        if self.step.kind == "normalize":
            lufs = self.values["mode"] == "loudness"
            self.rows["ceiling"].setEnabled(lufs)
            self.rows["target"].spin.setSuffix(" LUFS" if lufs else
                                               (" dBTP" if self.values["mode"] == "true_peak" else " dBFS"))

    def result_values(self) -> dict:
        return batch.clean_values(self.step.kind, self.values)


class FileList(QListWidget):
    """The list of files: drop files or folders on it (a folder adds its supported files)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.on_change = lambda: None

    def paths(self) -> list[str]:
        return [self.item(i).data(Qt.UserRole) for i in range(self.count())]

    def add_paths(self, paths) -> int:
        have = {os.path.normcase(p) for p in self.paths()}
        exts = AUDIO_EXT + VIDEO_EXT
        n = 0
        for p in paths:
            if os.path.isdir(p):
                found = [x for x, _r in batch.collect(batch.Source("folder", p, True))]
            elif p.lower().endswith(exts):
                found = [p]
            else:
                found = []
            for f in found:
                f = os.path.normpath(f)
                if os.path.normcase(f) in have:
                    continue
                have.add(os.path.normcase(f))
                it = QListWidgetItem(style.icon("fa6s.film" if f.lower().endswith(VIDEO_EXT) else "fa6s.music"),
                                     f)
                it.setData(Qt.UserRole, f)
                self.addItem(it)
                n += 1
        if n:
            self.on_change()
        return n

    def dragEnterEvent(self, e):                                     # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):                                      # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):                                          # noqa: N802
        self.add_paths([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])
        e.acceptProposedAction()


class BatchWindow(QDialog):
    """Not modal: the editor stays usable while a batch runs."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.cfg = win.cfg
        self.runner: batch.Runner | None = None
        self._preview = None                     # (path, Document) of the first file's first seconds
        self._shown_tasks = -1
        self.setWindowTitle(t("bt_title"))
        self.setObjectName("batchWindow")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.setMinimumSize(980, 700)
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)
        top = QHBoxLayout()
        top.addWidget(_head(t("bt_title")))
        top.addStretch(1)
        top.addWidget(QLabel(t("bt_preset")))
        self.presets = QComboBox()
        self.presets.setMinimumWidth(220)
        self.presets.activated.connect(self._preset_chosen)
        top.addWidget(self.presets)
        self.b_psave = QPushButton(style.icon("fa6s.floppy-disk"), t("bt_preset_save") + "…")
        self.b_psave.clicked.connect(self._save_preset)
        top.addWidget(self.b_psave)
        self.b_pmore = QToolButton()
        self.b_pmore.setIcon(style.icon("fa6s.ellipsis"))
        self.b_pmore.setPopupMode(QToolButton.InstantPopup)
        pm = QMenu(self.b_pmore)
        pm.addAction(style.icon("fa6s.folder-open"), t("bt_preset_open") + "…", self._open_preset_file)
        pm.addAction(style.icon("fa6s.file-export"), t("bt_preset_export") + "…", self._export_preset_file)
        pm.addSeparator()
        self.a_pdel = pm.addAction(style.icon("fa6s.trash-can"), t("bt_preset_del"), self._delete_preset)
        pm.addAction(style.icon("fa6s.folder"), t("bt_preset_folder"), lambda: winint.show_in_explorer(
            batch.presets_dir()))
        self.b_pmore.setMenu(pm)
        top.addWidget(self.b_pmore)
        lay.addLayout(top)

        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._source_page(), style.icon("fa6s.folder-tree"), t("bt_tab_source"))
        self.tabs.addTab(self._chain_page(), style.icon("fa6s.list-check"), t("bt_tab_chain"))
        self.tabs.addTab(self._output_page(), style.icon("fa6s.file-export"), t("bt_tab_output"))
        split.addWidget(self.tabs)
        split.addWidget(self._run_panel())
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        self.split = split
        lay.addWidget(split, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._poll)
        self.count_timer = QTimer(self)
        self.count_timer.setSingleShot(True)
        self.count_timer.setInterval(400)
        self.count_timer.timeout.connect(self._count_files)
        job = None
        last = self.cfg.get("batch_last")
        if last:
            try:
                job = batch.Job.from_dict(last)
            except (ValueError, TypeError):
                job = None
        if job is None:
            job = batch.Job(steps=[batch.Step("normalize")])
        self.set_job(job)
        self._fill_presets()
        geo = self.cfg.get("batch_geometry")
        if geo:
            from PySide6.QtCore import QByteArray
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        else:
            self.resize(1120, 800)
        sp = self.cfg.get("batch_split")
        if sp:
            self.split.setSizes([int(x) for x in sp])
        self._update_buttons()

    # ── the source ────────────────────────────────────────────
    def _source_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 10, 12, 10)
        self.r_folder = QRadioButton(t("bt_src_folder"))
        self.r_list = QRadioButton(t("bt_src_list"))
        g = QButtonGroup(w)
        g.addButton(self.r_folder)
        g.addButton(self.r_list)
        h = QHBoxLayout()
        h.addWidget(self.r_folder)
        h.addWidget(self.r_list)
        h.addStretch(1)
        v.addLayout(h)
        self.folder_box = QWidget()
        f = QFormLayout(self.folder_box)
        f.setContentsMargins(0, 0, 0, 0)
        f.setHorizontalSpacing(14)
        row = QHBoxLayout()
        self.src_folder = QLineEdit()
        self.src_folder.setPlaceholderText(t("bt_src_folder_ph"))
        self.src_folder.textChanged.connect(self._source_changed)
        row.addWidget(self.src_folder, 1)
        b = QPushButton(t("sv_browse"))
        b.clicked.connect(self._browse_source)
        row.addWidget(b)
        f.addRow(t("bt_folder"), row)
        self.src_recursive = QCheckBox(t("bt_recursive"))
        self.src_recursive.toggled.connect(self._source_changed)
        f.addRow("", self.src_recursive)
        self.src_ext = QLineEdit()
        self.src_ext.setPlaceholderText(t("bt_ext_ph"))
        self.src_ext.textChanged.connect(self._source_changed)
        f.addRow(t("bt_ext"), self.src_ext)
        v.addWidget(self.folder_box)
        self.list_box = QWidget()
        lv = QVBoxLayout(self.list_box)
        lv.setContentsMargins(0, 0, 0, 0)
        self.files = FileList()
        self.files.on_change = self._source_changed
        lv.addWidget(self.files, 1)
        lh = QHBoxLayout()
        b = QPushButton(style.icon("fa6s.plus"), t("bt_add_files") + "…")
        b.clicked.connect(self._add_files)
        lh.addWidget(b)
        b = QPushButton(style.icon("fa6s.folder-plus"), t("bt_add_folder") + "…")
        b.clicked.connect(self._add_folder)
        lh.addWidget(b)
        b = QPushButton(style.icon("fa6s.minus"), t("bt_remove"))
        b.clicked.connect(self._remove_files)
        lh.addWidget(b)
        b = QPushButton(t("bt_clear"))
        b.clicked.connect(lambda: (self.files.clear(), self._source_changed()))
        lh.addWidget(b)
        lh.addStretch(1)
        lh.addWidget(_hint(t("bt_drop_hint")))
        lv.addLayout(lh)
        v.addWidget(self.list_box, 1)
        tf = QFormLayout()
        tf.setHorizontalSpacing(14)
        th = QHBoxLayout()
        self.src_tracks = QComboBox()
        for k in ("default", "lang", "all"):
            self.src_tracks.addItem(t("bt_tracks_" + k), k)
        self.src_tracks.currentIndexChanged.connect(self._tracks_changed)
        th.addWidget(self.src_tracks)
        self.src_lang = QLineEdit()
        self.src_lang.setPlaceholderText("rus, ru")
        self.src_lang.setMaximumWidth(140)
        th.addWidget(self.src_lang)
        th.addStretch(1)
        tf.addRow(t("bt_tracks"), th)
        v.addLayout(tf)
        self.src_count = _hint("")
        v.addWidget(self.src_count)
        self.r_folder.toggled.connect(self._mode_changed)
        return w

    def _mode_changed(self, _on=None) -> None:
        folder = self.r_folder.isChecked()
        self.folder_box.setVisible(folder)
        self.list_box.setVisible(not folder)
        self.keep_structure.setEnabled(folder and self.r_other.isChecked())
        self._source_changed()

    def _tracks_changed(self, _i=None) -> None:
        self.src_lang.setEnabled(self.src_tracks.currentData() == "lang")

    def _source_changed(self, *_a) -> None:
        self.count_timer.start()
        self._preview_drop()
        self._example()

    def _count_files(self) -> None:
        try:
            n = len(batch.collect(self.job().source))
        except OSError:
            n = 0
        self.src_count.setText(t("bt_found", n))
        self._update_buttons()

    def _browse_source(self) -> None:
        p = QFileDialog.getExistingDirectory(self, t("bt_folder"), self.src_folder.text() or
                                             self.cfg.get("last_dir", ""))
        if p:
            self.src_folder.setText(os.path.normpath(p))

    def _add_files(self) -> None:
        exts = " ".join("*" + e for e in AUDIO_EXT + VIDEO_EXT)
        paths, _f = QFileDialog.getOpenFileNames(self, t("bt_add_files"), self.cfg.get("last_dir", ""),
                                                 f"{t('bt_supported')} ({exts})")
        if paths:
            self.cfg["last_dir"] = os.path.dirname(paths[0])
            self.files.add_paths(paths)

    def _add_folder(self) -> None:
        p = QFileDialog.getExistingDirectory(self, t("bt_add_folder"), self.cfg.get("last_dir", ""))
        if p:
            self.files.add_paths([p])

    def _remove_files(self) -> None:
        for it in self.files.selectedItems():
            self.files.takeItem(self.files.row(it))
        self._source_changed()

    def dragEnterEvent(self, e):                                     # noqa: N802
        if e.mimeData().hasUrls() and not self._running():
            e.acceptProposedAction()

    def dropEvent(self, e):                                          # noqa: N802
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        if len(paths) == 1 and os.path.isdir(paths[0]) and self.r_folder.isChecked():
            self.src_folder.setText(os.path.normpath(paths[0]))
        else:
            self.r_list.setChecked(True)
            self.files.add_paths(paths)
        self.tabs.setCurrentIndex(0)
        e.acceptProposedAction()

    # ── the chain ─────────────────────────────────────────────
    def _chain_page(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(12, 10, 12, 10)
        self.chain = QListWidget()
        self.chain.setDragDropMode(QAbstractItemView.InternalMove)
        self.chain.itemDoubleClicked.connect(lambda _it: self._edit_step())
        self.chain.itemChanged.connect(lambda _it: self._update_buttons())
        self.chain.currentRowChanged.connect(lambda _r: self._update_buttons())
        self.chain.model().rowsMoved.connect(lambda *_a: self._update_buttons())
        h.addWidget(self.chain, 1)
        col = QVBoxLayout()
        self.b_add = QPushButton(style.icon("fa6s.plus"), t("bt_add_step"))
        menu = QMenu(self.b_add)
        menu.addSection(t("m_process"))
        for k in batch.PROCESS_STEPS:
            menu.addAction(style.icon(STEP_ICONS[k]), batch.Step(k).title(), lambda k=k: self._add_step(k))
        menu.addSection(t("m_effects"))
        for k in batch.FX_STEPS:
            menu.addAction(style.icon(STEP_ICONS[k]), batch.Step(k).title(), lambda k=k: self._add_step(k))
        self.b_add.setMenu(menu)
        col.addWidget(self.b_add)
        self.b_edit = QPushButton(style.icon("fa6s.pen"), t("bt_edit_step") + "…")
        self.b_edit.clicked.connect(self._edit_step)
        col.addWidget(self.b_edit)
        self.b_del = QPushButton(style.icon("fa6s.trash-can"), t("bt_del_step"))
        self.b_del.clicked.connect(self._del_step)
        col.addWidget(self.b_del)
        self.b_up = QPushButton(style.icon("fa6s.arrow-up"), t("bt_up"))
        self.b_up.clicked.connect(lambda: self._move(-1))
        col.addWidget(self.b_up)
        self.b_down = QPushButton(style.icon("fa6s.arrow-down"), t("bt_down"))
        self.b_down.clicked.connect(lambda: self._move(1))
        col.addWidget(self.b_down)
        col.addSpacing(10)
        note = _hint(t("bt_chain_note"))
        note.setMaximumWidth(300)
        col.addWidget(note)
        col.addStretch(1)
        h.addLayout(col)
        return w

    def _step_item(self, s: batch.Step) -> QListWidgetItem:
        it = QListWidgetItem(style.icon(STEP_ICONS[s.kind]), "")
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
        it.setCheckState(Qt.Checked if s.enabled else Qt.Unchecked)
        it.setData(Qt.UserRole, s)
        self._label_item(it)
        return it

    @staticmethod
    def _label_item(it: QListWidgetItem) -> None:
        s = it.data(Qt.UserRole)
        summ = s.summary()
        it.setText(s.title() + (f"  —  {summ}" if summ else ""))

    def steps(self) -> list:
        out = []
        for i in range(self.chain.count()):
            it = self.chain.item(i)
            s = it.data(Qt.UserRole)
            out.append(batch.Step(s.kind, dict(s.values), it.checkState() == Qt.Checked))
        return out

    def _add_step(self, kind: str) -> None:
        s = batch.Step(kind)
        last = (self.cfg.get("fx_last") or {}).get(kind) if kind in batch.FX_STEPS else None
        if last:
            s = batch.Step(kind, {**s.values, **last})
        row = self.chain.currentRow() + 1 if self.chain.currentRow() >= 0 else self.chain.count()
        self.chain.insertItem(row, self._step_item(s))
        self.chain.setCurrentRow(row)
        if batch.step_params(kind):
            self._edit_step()
        self._update_buttons()

    def _edit_step(self) -> None:
        it = self.chain.currentItem()
        if it is None or self._running():
            return
        s: batch.Step = it.data(Qt.UserRole)
        if s.kind in batch.FX_STEPS:
            values = self._edit_fx(s)
        else:
            if not batch.step_params(s.kind):
                return
            dlg = StepDialog(s, self)
            values = dlg.result_values() if dlg.exec() else None
        if values is not None:
            it.setData(Qt.UserRole, batch.Step(s.kind, values, it.checkState() == Qt.Checked))
            self._label_item(it)

    def _edit_fx(self, s: batch.Step):
        from . import fxui
        from .doc import new_document
        d, name = self._preview_doc()
        if d is None:
            d = new_document(self.win.session.folder(), 48000, 2)
            name = ""
        dlg = fxui.EffectDialog(self.win, s.kind, d, 0, d.frames, True, {"values": s.values, "file": name})
        dlg.setParent(self, dlg.windowFlags())
        ok = dlg.exec()
        if not name:
            d.close()
        return dlg.result_values() if ok else None

    def _preview_doc(self):
        """The first seconds of the first file (decoded once per first file)."""
        try:
            files = batch.collect(self.job().source)
        except OSError:
            files = []
        if not files:
            return None, ""
        path = files[0][0]
        if self._preview and self._preview[0] == path:
            return self._preview[1], os.path.basename(path)
        self._preview_drop()
        from PySide6.QtWidgets import QApplication
        from . import ff
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            info = ff.probe(path)
            tr = batch.pick_tracks(info, self.job().source)[0][0]
            d = media.open_preview(path, self.win.session.folder(), tr.pos, 30.0, info)
        except Exception:                                          # noqa: BLE001 — then no preview
            return None, ""
        finally:
            QApplication.restoreOverrideCursor()
        self._preview = (path, d)
        return d, os.path.basename(path)

    def _preview_drop(self) -> None:
        if self._preview:
            try:
                self._preview[1].close()
            except Exception:                                      # noqa: BLE001
                pass
            self._preview = None

    def _del_step(self) -> None:
        r = self.chain.currentRow()
        if r >= 0:
            self.chain.takeItem(r)
        self._update_buttons()

    def _move(self, k: int) -> None:
        r = self.chain.currentRow()
        n = r + k
        if r < 0 or not 0 <= n < self.chain.count():
            return
        it = self.chain.takeItem(r)
        self.chain.insertItem(n, it)
        self.chain.setCurrentRow(n)

    # ── the output ────────────────────────────────────────────
    def _output_page(self) -> QWidget:
        w = QWidget()
        g = QGridLayout(w)
        g.setContentsMargins(12, 10, 12, 10)
        g.setHorizontalSpacing(14)
        f = QFormLayout()
        f.setHorizontalSpacing(14)
        self.o_fmt = QComboBox()
        self.o_fmt.addItem(t("bt_fmt_source"), "source")
        for k, fm in media.FORMATS.items():
            self.o_fmt.addItem(fm.name, k)
        self.o_fmt.currentIndexChanged.connect(self._fmt_changed)
        f.addRow(t("sv_format"), self.o_fmt)
        self.o_opt = QComboBox()
        self.o_opt.currentIndexChanged.connect(lambda _i: self._example())
        f.addRow(t("sv_option"), self.o_opt)
        fw = QWidget()
        fv = QVBoxLayout(fw)
        fv.setContentsMargins(0, 0, 0, 0)
        self.r_same = QRadioButton(t("bt_out_same"))
        self.r_other = QRadioButton(t("bt_out_other"))
        bg = QButtonGroup(w)
        bg.addButton(self.r_same)
        bg.addButton(self.r_other)
        fv.addWidget(self.r_same)
        oh = QHBoxLayout()
        oh.addWidget(self.r_other)
        self.o_folder = QLineEdit()
        self.o_folder.textChanged.connect(lambda _s: self._example())
        oh.addWidget(self.o_folder, 1)
        b = QPushButton(t("sv_browse"))
        b.clicked.connect(self._browse_out)
        oh.addWidget(b)
        fv.addLayout(oh)
        self.keep_structure = QCheckBox(t("bt_keep_structure"))
        fv.addWidget(self.keep_structure)
        self.r_other.toggled.connect(self._out_mode)
        f.addRow(t("bt_out_folder"), fw)
        nh = QHBoxLayout()
        self.o_template = QLineEdit()
        self.o_template.textChanged.connect(lambda _s: self._example())
        nh.addWidget(self.o_template, 2)
        nh.addWidget(QLabel(t("bt_suffix")))
        self.o_suffix = QLineEdit()
        self.o_suffix.textChanged.connect(lambda _s: self._example())
        nh.addWidget(self.o_suffix, 1)
        f.addRow(t("bt_template"), nh)
        f.addRow("", _hint(t("bt_template_hint")))
        self.o_example = QLabel("")
        self.o_example.setObjectName("hint")
        self.o_example.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f.addRow(t("bt_example"), self.o_example)
        self.o_exists = QComboBox()
        for k in ("rename", "skip", "replace"):
            self.o_exists.addItem(t("bt_exists_" + k), k)
        f.addRow(t("bt_exists"), self.o_exists)
        self.o_tags = QCheckBox(t("bt_keep_tags"))
        self.o_marks = QCheckBox(t("bt_keep_markers"))
        f.addRow("", self.o_tags)
        f.addRow("", self.o_marks)
        g.addLayout(f, 0, 0)
        f2 = QFormLayout()
        f2.setHorizontalSpacing(14)
        self.o_workers = QSpinBox()
        self.o_workers.setRange(1, max(1, os.cpu_count() or 1))
        f2.addRow(t("bt_workers"), self.o_workers)
        f2.addRow("", _hint(t("bt_workers_hint", batch.default_workers())))
        self.o_measure = QCheckBox(t("bt_measure"))
        f2.addRow("", self.o_measure)
        g.addLayout(f2, 0, 1)
        g.setColumnStretch(0, 3)
        g.setColumnStretch(1, 2)
        g.setRowStretch(1, 1)
        return w

    def _fmt_changed(self, _i=None, option: str = "") -> None:
        k = self.o_fmt.currentData()
        self.o_opt.blockSignals(True)
        self.o_opt.clear()
        if k == "source":
            self.o_opt.addItem(t("bt_opt_source"), "")
            self.o_opt.setEnabled(False)
        else:
            self.o_opt.setEnabled(True)
            fm = media.FORMATS[k]
            for o in fm.options:
                self.o_opt.addItem(option_text(k, o), o)
            self.o_opt.setCurrentIndex(max(0, self.o_opt.findData(option or fm.default)))
        self.o_opt.blockSignals(False)
        self._example()

    def _out_mode(self, _on=None) -> None:
        other = self.r_other.isChecked()
        self.o_folder.setEnabled(other)
        self.keep_structure.setEnabled(other and self.r_folder.isChecked())
        self._example()

    def _browse_out(self) -> None:
        p = QFileDialog.getExistingDirectory(self, t("bt_out_folder"), self.o_folder.text() or
                                             self.cfg.get("save_dir", ""))
        if p:
            self.o_folder.setText(os.path.normpath(p))
            self.r_other.setChecked(True)

    def _example(self) -> None:
        if not hasattr(self, "o_example"):
            return
        import datetime
        o = self.job().output
        k = o.format if o.format != "source" else "flac"
        name = batch.clean_name(batch.render_name(o.template, t("bt_example_name"), 1, datetime.date.today().isoformat(),
                                                  1, "rus") + o.suffix)
        self.o_example.setText(name + media.FORMATS[k].ext)

    # ── the job ⇄ the fields ──────────────────────────────────
    def job(self) -> batch.Job:
        src = batch.Source("folder" if self.r_folder.isChecked() else "list", self.src_folder.text().strip(),
                           self.src_recursive.isChecked(), self.src_ext.text().strip(), self.files.paths(),
                           self.src_tracks.currentData() or "default", self.src_lang.text().strip() or "rus")
        out = batch.Output(self.o_fmt.currentData() or "flac", self.o_opt.currentData() or "",
                           "other" if self.r_other.isChecked() else "same", self.o_folder.text().strip(),
                           self.keep_structure.isChecked(), self.o_template.text() or "{name}", self.o_suffix.text(),
                           self.o_exists.currentData() or "rename", self.o_tags.isChecked(), self.o_marks.isChecked())
        workers = self.o_workers.value()
        return batch.Job(src, self.steps(), out, 0 if workers == batch.default_workers() else workers,
                         self.o_measure.isChecked())

    def set_job(self, j: batch.Job) -> None:
        s, o = j.source, j.output
        (self.r_list if s.mode == "list" else self.r_folder).setChecked(True)
        self.src_folder.setText(s.folder)
        self.src_recursive.setChecked(s.recursive)
        self.src_ext.setText(s.extensions)
        self.files.clear()
        self.files.add_paths(s.files)
        self.src_tracks.setCurrentIndex(max(0, self.src_tracks.findData(s.tracks)))
        self.src_lang.setText(s.lang)
        self._tracks_changed()
        self.chain.clear()
        for st in j.steps:
            self.chain.addItem(self._step_item(st))
        self.o_fmt.setCurrentIndex(max(0, self.o_fmt.findData(o.format)))
        self._fmt_changed(option=o.option)
        (self.r_other if o.folder_mode == "other" else self.r_same).setChecked(True)
        self.o_folder.setText(o.folder)
        self.keep_structure.setChecked(o.keep_structure)
        self.o_template.setText(o.template)
        self.o_suffix.setText(o.suffix)
        self.o_exists.setCurrentIndex(max(0, self.o_exists.findData(o.exists)))
        self.o_tags.setChecked(o.keep_tags)
        self.o_marks.setChecked(o.keep_markers)
        self.o_workers.setValue(j.workers or batch.default_workers())
        self.o_measure.setChecked(j.measure)
        self._mode_changed()
        self._out_mode()
        self._count_files()

    # ── presets ───────────────────────────────────────────────
    def _fill_presets(self, select: str = "") -> None:
        self.presets.clear()
        self.presets.addItem(t("bt_preset_none"), "")
        for name in batch.list_presets():
            self.presets.addItem(name, name)
        if select:
            self.presets.setCurrentIndex(max(0, self.presets.findData(select)))
        self.a_pdel.setEnabled(bool(self.presets.currentData()))

    def _preset_path(self, name: str) -> str:
        return os.path.join(batch.presets_dir(), batch.clean_name(name) + batch.PRESET_EXT)

    def _preset_chosen(self, _i: int) -> None:
        name = self.presets.currentData()
        self.a_pdel.setEnabled(bool(name))
        if name:
            self.load_preset_file(self._preset_path(name))

    def load_preset_file(self, path: str) -> bool:
        try:
            self.set_job(batch.Job.load(path))
            return True
        except (OSError, ValueError, TypeError) as e:
            QMessageBox.warning(self, APP_TITLE, t("bt_preset_bad", os.path.basename(path), str(e)))
            return False

    def _save_preset(self) -> None:
        cur = self.presets.currentData() or ""
        name, ok = QInputDialog.getText(self, t("bt_preset_save"), t("bt_preset_name"), text=cur)
        name = batch.clean_name((name or "").strip()) if ok and (name or "").strip() else ""
        if not name:
            return
        self.save_preset(name)

    def save_preset(self, name: str) -> None:
        self.job().save(self._preset_path(name))
        self._fill_presets(name)

    def _delete_preset(self) -> None:
        name = self.presets.currentData()
        if name and QMessageBox.question(self, APP_TITLE, t("bt_preset_del_q", name)) == QMessageBox.Yes:
            try:
                os.remove(self._preset_path(name))
            except OSError:
                pass
            self._fill_presets()

    def _open_preset_file(self) -> None:
        p, _f = QFileDialog.getOpenFileName(self, t("bt_preset_open"), batch.presets_dir(),
                                            f"{t('bt_preset_filter')} (*{batch.PRESET_EXT})")
        if p:
            self.load_preset_file(p)

    def _export_preset_file(self) -> None:
        p, _f = QFileDialog.getSaveFileName(self, t("bt_preset_export"), batch.presets_dir(),
                                            f"{t('bt_preset_filter')} (*{batch.PRESET_EXT})")
        if p:
            if not p.lower().endswith(batch.PRESET_EXT):
                p += batch.PRESET_EXT
            try:
                self.job().save(p)
            except OSError as e:
                from .errors import human
                QMessageBox.warning(self, APP_TITLE, human(e))

    # ── the run ───────────────────────────────────────────────
    def _run_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels([t("bcol_" + c) for c in COLS])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(7, QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(self._row_clicked)
        v.addWidget(self.table, 1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        v.addWidget(self.bar)
        self.info = QLabel(t("bt_ready"))
        self.info.setWordWrap(True)
        v.addWidget(self.info)
        h = QHBoxLayout()
        h.addStretch(1)
        self.b_report = QPushButton(style.icon("fa6s.file-lines"), t("bt_report") + "…")
        rm = QMenu(self.b_report)
        rm.addAction(t("bt_report_csv") + "…", lambda: self._save_report("csv"))
        rm.addAction(t("bt_report_txt") + "…", lambda: self._save_report("txt"))
        self.b_report.setMenu(rm)
        h.addWidget(self.b_report)
        self.b_open_out = QPushButton(style.icon("fa6s.folder-open"), t("bt_open_out"))
        self.b_open_out.clicked.connect(self._open_out)
        h.addWidget(self.b_open_out)
        h.addSpacing(16)
        self.b_start = QPushButton(style.icon("fa6s.play"), t("bt_start"))
        self.b_start.setObjectName("primary")
        self.b_start.clicked.connect(self.start)
        h.addWidget(self.b_start)
        self.b_pause = QPushButton(style.icon("fa6s.pause"), t("pause"))
        self.b_pause.setCheckable(True)
        self.b_pause.toggled.connect(self._pause)
        h.addWidget(self.b_pause)
        self.b_cancel = QPushButton(style.icon("fa6s.stop"), t("bt_cancel"))
        self.b_cancel.clicked.connect(self.cancel)
        h.addWidget(self.b_cancel)
        self.b_close = QPushButton(t("close"))
        self.b_close.clicked.connect(self.close)
        h.addWidget(self.b_close)
        v.addLayout(h)
        return w

    def _running(self) -> bool:
        return self.runner is not None and self.runner.running

    def _update_buttons(self) -> None:
        if not hasattr(self, "b_start"):
            return
        run = self._running()
        has_steps = any(s.enabled for s in self.steps())
        self.b_start.setEnabled(not run)
        self.b_start.setToolTip("" if has_steps else t("bt_no_steps_tip"))
        self.b_pause.setEnabled(run)
        self.b_cancel.setEnabled(run)
        done = self.runner is not None and not run and bool(self.runner.tasks)
        self.b_report.setEnabled(done)
        self.b_open_out.setEnabled(done and any(x.status == "ok" for x in self.runner.tasks))
        for wdg in (self.tabs, self.presets, self.b_psave, self.b_pmore):
            wdg.setEnabled(not run)
        r = self.chain.currentRow()
        for b in (self.b_edit, self.b_del):
            b.setEnabled(r >= 0)
        self.b_up.setEnabled(r > 0)
        self.b_down.setEnabled(0 <= r < self.chain.count() - 1)

    def start(self) -> None:
        if self._running():
            return
        job = self.job()
        if job.source.mode == "folder" and not os.path.isdir(job.source.folder):
            QMessageBox.information(self, APP_TITLE, t("bt_need_folder"))
            self.tabs.setCurrentIndex(0)
            return
        if not batch.collect(job.source):
            QMessageBox.information(self, APP_TITLE, t("bt_no_files"))
            self.tabs.setCurrentIndex(0)
            return
        if job.output.folder_mode == "other" and not job.output.folder:
            QMessageBox.information(self, APP_TITLE, t("bt_need_out"))
            self.tabs.setCurrentIndex(2)
            return
        if not any(s.enabled for s in job.steps) and \
                QMessageBox.question(self, APP_TITLE, t("bt_no_steps_q")) != QMessageBox.Yes:
            return
        miss = [s for s in job.steps if s.enabled and s.kind == "nr" and not os.path.isfile(s.values.get("profile", ""))]
        if miss:
            QMessageBox.information(self, APP_TITLE, t("bt_err_no_profile"))
            self.tabs.setCurrentIndex(1)
            return
        self._preview_drop()
        self.cfg["batch_last"] = job.to_dict()
        config.save(self.cfg)
        self.runner = batch.Runner(job, config.work_root(self.cfg))
        self.table.setRowCount(0)
        self._shown_tasks = -1
        self.b_pause.setChecked(False)
        self.runner.start()
        self.timer.start()
        self._update_buttons()
        self._poll()

    def _pause(self, on: bool) -> None:
        if self.runner is not None:
            self.runner.pause(on)
        self.b_pause.setText(t("bt_resume") if on else t("pause"))
        self.b_pause.setIcon(style.icon("fa6s.play" if on else "fa6s.pause"))

    def cancel(self) -> None:
        if self._running():
            self.runner.cancel()
            self.info.setText(t("bt_cancelling"))

    def _poll(self) -> None:
        r = self.runner
        if r is None:
            return
        snap = r.snapshot()
        tasks = snap["tasks"]
        if len(tasks) != self._shown_tasks:
            self.table.setRowCount(len(tasks))
            for i, x in enumerate(tasks):
                self._set_cell(i, 0, str(x.n))
                self._set_cell(i, 1, os.path.basename(x.src) + (f"  [{x.track_text}]" if x.track_text else ""),
                               x.src + (f"\n→ {x.out}" if x.out else ""))
            self._shown_tasks = len(tasks)
        for i, x in enumerate(tasks):
            self._set_cell(i, 2, t("bst_" + x.status), color=STATUS_COLORS.get(x.status))
            pc = "" if x.status in ("wait", "skip") or (x.status == "error" and not x.seconds) else \
                f"{int(round((1.0 if x.status == 'ok' else x.frac) * 100))} %"
            self._set_cell(i, 3, pc)
            self._set_cell(i, 4, _pair(x.before.get("lufs"), x.after.get("lufs")))
            self._set_cell(i, 5, _pair(x.before.get("peak"), x.after.get("peak")))
            self._set_cell(i, 6, f"{x.seconds:.1f} s" if x.seconds else "")
            note = "; ".join(filter(None, [x.error] + list(x.notes)))
            self._set_cell(i, 7, note, note, STATUS_COLORS["error"] if x.error else None)
        self.bar.setValue(int(snap["frac"] * 1000))
        c = snap["counts"]
        el = _fmt_dur(snap["elapsed"])
        if snap["phase"] == "plan":
            self.info.setText(t("bt_planning", int(r.plan_frac * 100)))
        elif r.running:
            left = ""
            if snap["frac"] > 0.02:
                left = " · " + t("bt_left", _fmt_dur(snap["elapsed"] * (1 - snap["frac"]) / snap["frac"]))
            self.info.setText(t("bt_progress", c["ok"] + c["error"] + c["skip"], len(tasks), c["error"], el) + left +
                              (" · " + t("bt_paused") if snap["paused"] else ""))
        else:
            self.timer.stop()
            s = batch.summary(tasks, snap["elapsed"])
            text = t("bt_summary", s["ok"], s["error"], s["skip"], s["cancel"], _fmt_dur(s["elapsed"]),
                     _fmt_dur(s["audio"]))
            if r.error:
                text += " · " + r.error
            self.info.setText(text)
            self.bar.setValue(1000 if snap["phase"] == "done" else self.bar.value())
            self._update_buttons()
            if self.isVisible():
                self.win._toast(text)

    def _set_cell(self, r: int, c: int, text: str, tip: str = "", color: str | None = None) -> None:
        it = self.table.item(r, c)
        if it is None:
            it = QTableWidgetItem()
            self.table.setItem(r, c, it)
        if it.text() != text:
            it.setText(text)
        if tip and it.toolTip() != tip:
            it.setToolTip(tip)
        if color:
            from PySide6.QtGui import QColor
            it.setForeground(QColor(color))

    def _row_clicked(self, r: int, _c: int) -> None:
        if self.runner is None or r >= len(self.runner.tasks):
            return
        x = self.runner.tasks[r]
        if x.status == "ok" and os.path.isfile(x.out):
            winint.show_in_explorer(x.out)

    def _out_folder(self) -> str:
        oks = [x for x in (self.runner.tasks if self.runner else []) if x.status == "ok"]
        if not oks:
            return ""
        j = self.runner.job
        if j.output.folder_mode == "other" and j.output.folder:
            return j.output.folder
        return os.path.dirname(oks[0].out)

    def _open_out(self) -> None:
        p = self._out_folder()
        if p:
            winint.show_in_explorer(p)

    def _save_report(self, kind: str) -> None:
        if self.runner is None:
            return
        import datetime
        base = self._out_folder() or self.cfg.get("save_dir", "")
        name = f"ArtCore Sound batch {datetime.datetime.now():%Y-%m-%d %H-%M}.{kind}"
        filt = "CSV (*.csv)" if kind == "csv" else f"{t('bt_report_txt')} (*.txt)"
        p, _f = QFileDialog.getSaveFileName(self, t("bt_report"), os.path.join(base, name), filt)
        if not p:
            return
        self.save_report(p, kind)

    def save_report(self, path: str, kind: str) -> None:
        r = self.runner
        snap = r.snapshot()
        try:
            if kind == "csv":
                batch.save_csv(snap["tasks"], path)
            else:
                batch.save_txt(snap["tasks"], path, snap["elapsed"], r.job)
        except OSError as e:
            from .errors import human
            QMessageBox.warning(self, APP_TITLE, human(e))
            return
        self.win._toast(t("bt_report_saved", os.path.basename(path)))

    # ── closing ───────────────────────────────────────────────
    def closeEvent(self, ev) -> None:                                # noqa: N802
        if self._running():
            if QMessageBox.question(self, APP_TITLE, t("bt_close_q")) != QMessageBox.Yes:
                ev.ignore()
                return
            self.runner.cancel()
            self.runner.wait(30)
        self.timer.stop()
        self._preview_drop()
        self.cfg["batch_last"] = self.job().to_dict()
        self.cfg["batch_geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        self.cfg["batch_split"] = self.split.sizes()
        config.save(self.cfg)
        super().closeEvent(ev)

    def reject(self) -> None:
        self.close()
