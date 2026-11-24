"""VST3 in the window (2.0): the body of a plugin's effect window (its parameters, its own window, channels,
.vstpreset files), the «Plugins VST3» menu, and the VST3 page of the settings (folders, scanning, the list of
plugins and of those that did not load).

The effect window keeps a host of its own (a process, like the preview's) for the plugin's parameters and its own
window: a change there reaches the preview within a fraction of a second (the state is read back every 250 ms)."""
from __future__ import annotations

import os
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QListWidget, QMenu, QMessageBox, QPushButton,
                               QScrollArea, QSlider, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import APP_TITLE, config, style
from .fx import vst3 as fxv
from .i18n import t
from .vst import scan

PARAMS_SHOWN = 400                   # a plugin with more parameters: the first ones (its own window has all)


def channels_text(chs: list) -> str:
    names = {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}
    return ", ".join(names.get(c, str(c)) for c in chs) if chs else "—"


# ── the body of a plugin's window ─────────────────────────────
class _Loader(QObject):
    done = Signal(object, str)


class VstBody(QWidget):
    """In EffectDialog (key "vst3"): the plugin's parameters and its own window. dlg.values holds the plugin's
    state (base64) — what the preview, a preset and Apply get."""

    def __init__(self, dlg):
        super().__init__(dlg)
        self.dlg = dlg
        self.host = None
        self.params: list[dict] = []
        self.ctrls: dict = {}                        # key → (control, value label)
        self.pending: dict = {}                      # parameters changed here since the state was read
        self.error = ""
        self._ticks = 0
        self._editor = False
        self._shown = None                           # the preview's processor whose channels are shown
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        ref = dlg.values["plugin"]
        self.info = QLabel(" · ".join(x for x in (ref.get("maker"), os.path.basename(ref.get("path", ""))) if x))
        self.info.setObjectName("hint")
        v.addWidget(self.info)
        row = QHBoxLayout()
        self.b_editor = QPushButton(style.icon("fa6s.window-maximize"), t("vst_editor"))
        self.b_editor.setCheckable(True)
        self.b_editor.setToolTip(t("vst_editor_tip"))
        self.b_editor.toggled.connect(self._editor_toggled)
        row.addWidget(self.b_editor)
        self.b_import = QPushButton(t("vst_import"))
        self.b_import.clicked.connect(self._import)
        self.b_export = QPushButton(t("vst_export"))
        self.b_export.clicked.connect(self._export)
        row.addWidget(self.b_import)
        row.addWidget(self.b_export)
        row.addStretch(1)
        v.addLayout(row)
        # the channels
        form = QFormLayout()
        self.mode = QComboBox()
        p = dlg.eff.by_key["mode"]
        for val, key in p.choices:
            self.mode.addItem(t(key), val)
        self.mode.setCurrentIndex(max(0, self.mode.findData(dlg.values["mode"])))
        self.mode.setToolTip(t("vst_mode_tip"))
        self.mode.currentIndexChanged.connect(self._channels_changed)
        form.addRow(t("vst_mode"), self.mode)
        self.use_boxes: list[QCheckBox] = []
        labels = list(dlg.d.labels) if dlg.d is not None else []
        if len(labels) > 1:
            box = QHBoxLayout()
            use = dlg.values["use"]
            for i, lab in enumerate(labels):
                c = QCheckBox(lab)
                c.setChecked(not use or i in use)
                c.toggled.connect(self._channels_changed)
                self.use_boxes.append(c)
                box.addWidget(c)
            box.addStretch(1)
            form.addRow(t("vst_use"), box)
        v.addLayout(form)
        self.taken = QLabel("")
        self.taken.setObjectName("hint")
        v.addWidget(self.taken)
        # the parameters
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setMinimumHeight(260)
        self.area.setObjectName("vstParams")
        self.area.setStyleSheet(f"QScrollArea#vstParams {{ background: {style.PANEL}; border: 1px solid "
                                f"{style.BORDER}; border-radius: 8px; }}")
        self.area.viewport().setAutoFillBackground(False)
        self.grid_w = QWidget()
        self.grid_w.setObjectName("vstGrid")
        self.grid_w.setStyleSheet("QWidget#vstGrid { background: transparent; }")
        self.grid = QGridLayout(self.grid_w)
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(4)
        self.area.setWidget(self.grid_w)
        v.addWidget(self.area, 1)
        self.note = QLabel(t("vst_loading"))
        self.note.setObjectName("hint")
        self.note.setWordWrap(True)
        v.addWidget(self.note)
        for w in (self.b_editor, self.b_import, self.b_export):
            w.setEnabled(False)
        self._loader = _Loader()
        self._loader.done.connect(self._loaded)
        self._load()

    # ── the host ──────────────────────────────────────────────
    def _load(self) -> None:
        dlg = self.dlg
        ref = dict(dlg.values["plugin"])
        rate = dlg.d.rate if dlg.d is not None and dlg.d.rate else 48000
        state, params = fxv.unb64(dlg.values["state"]), dict(dlg.values["params"])

        def work():
            from .vst.client import Host
            host = None
            try:
                r = fxv.resolve(ref)
                host = Host()
                try:
                    host.load(r["path"], r.get("name") or "", rate, 2, "auto")
                except Exception:                        # noqa: BLE001 — a mono-only plugin
                    host.close()
                    host = Host()
                    host.load(r["path"], r.get("name") or "", rate, 1, "auto")
                if state:
                    host.set(state=state)
                if params:
                    host.set(params=params)
                got = host.get()
                self._loader.done.emit((host, got), "")
            except Exception as e:                       # noqa: BLE001
                if host is not None:
                    host.close()
                self._loader.done.emit(None, str(e) or type(e).__name__)
        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def wait_loaded(self, timeout: float = 60.0) -> None:
        """Tests: until the plugin is loaded (or failed)."""
        from PySide6.QtWidgets import QApplication
        import time
        end = time.monotonic() + timeout
        while self.host is None and not self.error and time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.01)

    def _loaded(self, res, err: str) -> None:
        if self.dlg.vst is not self:                      # closed meanwhile
            if res is not None:
                res[0].close()
            return
        if res is None:
            self._fail(err)
            return
        self.host, got = res
        if not self.dlg.values["state"]:
            self.dlg.values["state"] = fxv.b64(got["state"])
        self._build(got["params"])
        for w in (self.b_editor, self.b_import, self.b_export):
            w.setEnabled(True)
        self.note.setText(t("vst_params_n", len(self.params)) if len(self.params) <= PARAMS_SHOWN else
                          t("vst_params_many", len(self.params), PARAMS_SHOWN))

    def _fail(self, err: str) -> None:
        self.error = err
        self.note.setText(t("fx_failed", err))
        self.dlg.status.setText(t("fx_failed", err))
        for w in (self.b_editor, self.b_import, self.b_export):
            w.setEnabled(False)
        self.grid_w.setEnabled(False)

    def _call(self, fn, *a):
        """A host command; the host gone (the plugin crashed): said once, the window stays."""
        if self.host is None or self.error:
            return None
        try:
            return fn(*a)
        except Exception as e:                           # noqa: BLE001
            self._fail(str(e) or type(e).__name__)
            return None

    # ── the parameters ────────────────────────────────────────
    def _build(self, params: list[dict]) -> None:
        while self.grid.count():
            it = self.grid.takeAt(0)
            if it.widget() is not None:
                it.widget().deleteLater()
        self.ctrls.clear()
        self.params = params
        for r, p in enumerate(params[:PARAMS_SHOWN]):
            name = QLabel(p["name"] or p["key"])
            self.grid.addWidget(name, r, 0)
            if p["kind"] == "bool":
                c = QCheckBox()
                c.setChecked(p["raw"] >= 0.5)
                c.toggled.connect(lambda on, k=p["key"]: self._moved(k, 1.0 if on else 0.0))
            elif p["kind"] == "choice" and p.get("choices"):
                c = QComboBox()
                ch = p["choices"]
                c.addItems(ch)
                c.setCurrentIndex(_index(p["raw"], len(ch)))
                c.currentIndexChanged.connect(lambda i, k=p["key"], n=len(ch): self._moved(k, i / max(1, n - 1)))
            else:
                c = QSlider(Qt.Horizontal)
                steps = p["steps"] if 2 <= p["steps"] <= 1001 else 1001
                c.setRange(0, steps - 1)
                c.setValue(round(p["raw"] * (steps - 1)))
                c.setMinimumWidth(240)
                c.valueChanged.connect(lambda i, k=p["key"], n=steps: self._moved(k, i / (n - 1)))
            self.grid.addWidget(c, r, 1)
            val = QLabel(_value_text(p))
            val.setMinimumWidth(90)
            self.grid.addWidget(val, r, 2)
            self.ctrls[p["key"]] = (c, val)
        self.grid.setColumnStretch(1, 1)
        self.grid.setRowStretch(len(params[:PARAMS_SHOWN]), 1)

    def _show(self, params: list[dict]) -> None:
        """New values (the plugin's window, a preset) into the controls, without echoing them back."""
        self.params = params
        for p in params[:PARAMS_SHOWN]:
            got = self.ctrls.get(p["key"])
            if got is None:
                continue
            c, val = got
            c.blockSignals(True)
            if isinstance(c, QCheckBox):
                c.setChecked(p["raw"] >= 0.5)
            elif isinstance(c, QComboBox):
                c.setCurrentIndex(_index(p["raw"], c.count()))
            else:
                c.setValue(round(p["raw"] * c.maximum()))
            c.blockSignals(False)
            val.setText(_value_text(p))

    def _moved(self, key: str, raw: float) -> None:
        text = self._call(self.host.param, key, raw) if self.host is not None else None
        if text is None:
            return
        got = self.ctrls.get(key)
        if got is not None:
            unit = next((p.get("unit", "") for p in self.params if p["key"] == key), "")
            got[1].setText(f"{text} {unit}".strip())
        self.pending[key] = raw
        self.dlg.values["params"] = dict(self.pending)       # heard at once; the state follows at the next read
        self.dlg._push()

    def read_state(self) -> bool:
        """The plugin's state back into the values (after its window or a parameter changed it)."""
        got = self._call(self.host.get) if self.host is not None else None
        if got is None:
            return False
        if self._editor and not got.get("editor"):
            self._set_editor_button(False)
            if got.get("editor_error"):
                self.dlg.status.setText(t("vst_no_editor", got["editor_error"]))
        self._editor = bool(got.get("editor"))
        state = fxv.b64(got["state"])
        changed = state != self.dlg.values["state"]
        self.dlg.values["state"] = state
        self.dlg.values["params"] = {}
        self.pending.clear()
        if changed:
            self._show(got["params"])
            self.dlg._push()
        return changed

    def tick(self) -> None:
        self._ticks += 1
        chain = self.dlg.chain
        if chain is not None and (chain.proc is not self._shown or self._ticks % 12 == 0):
            self._shown = chain.proc
            self.show_taken(chain.proc)
        if self._ticks % 6 == 0 and self.host is not None and (self._editor or self.pending):
            self.read_state()

    def values_set(self) -> None:
        """A preset chosen: its state into the plugin and the controls."""
        v = self.dlg.values
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(max(0, self.mode.findData(v["mode"])))
        self.mode.blockSignals(False)
        for i, c in enumerate(self.use_boxes):
            c.blockSignals(True)
            c.setChecked(not v["use"] or i in v["use"])
            c.blockSignals(False)
        if self.host is None:
            return
        state = fxv.unb64(v["state"])
        if state:
            self._call(self.host.set, state, None)
        if v["params"]:
            self._call(self.host.set, None, dict(v["params"]))
        self.pending.clear()
        got = self._call(self.host.get)
        if got is not None:
            v["state"], v["params"] = fxv.b64(got["state"]), {}
            self._show(got["params"])

    # ── the channels ──────────────────────────────────────────
    def _channels_changed(self, *_a) -> None:
        v = self.dlg.values
        v["mode"] = self.mode.currentData()
        if self.use_boxes:
            on = [i for i, c in enumerate(self.use_boxes) if c.isChecked()]
            if not on:                                   # not none: the last one stays
                sender = self.sender()
                if isinstance(sender, QCheckBox):
                    sender.blockSignals(True)
                    sender.setChecked(True)
                    sender.blockSignals(False)
                on = [i for i, c in enumerate(self.use_boxes) if c.isChecked()]
            v["use"] = [] if len(on) == len(self.use_boxes) else on
        self.dlg._push()

    def show_taken(self, proc) -> None:
        """What the preview's processor made of the channels (pairs, each…)."""
        if proc is None or not getattr(proc, "groups", None):
            return
        labels = list(self.dlg.d.labels)
        parts = [" ".join(labels[c] for c in g) for g in proc.groups]
        self.taken.setText(t("vst_taken", t("vst_mode_" + proc.mode_taken), " | ".join(parts)))

    # ── its own window, .vstpreset ────────────────────────────
    def _editor_toggled(self, on: bool) -> None:
        if self.host is None:
            return
        if self._call(self.host.editor, on) is None and self.error:
            self._set_editor_button(False)
            return
        self._editor = on
        if not on:
            self.read_state()

    def _set_editor_button(self, on: bool) -> None:
        self.b_editor.blockSignals(True)
        self.b_editor.setChecked(on)
        self.b_editor.blockSignals(False)
        self._editor = on

    def _import(self) -> None:
        start = self.dlg.win.cfg.get("vst_preset_dir") or ""
        path, _f = QFileDialog.getOpenFileName(self, t("vst_import"), start, t("vst_preset_filter") + " (*.vstpreset)")
        if path:
            self.import_preset(path)

    def import_preset(self, path: str) -> bool:
        got = self._call(self.host.preset_load, path) if self.host is not None else None
        if got is None:
            if not self.error:
                QMessageBox.warning(self, APP_TITLE, t("vst_preset_bad", os.path.basename(path)))
            return False
        self.dlg.win.cfg["vst_preset_dir"] = os.path.dirname(path)
        self.dlg.values["state"], self.dlg.values["params"] = fxv.b64(got["state"]), {}
        self.pending.clear()
        self._show(got["params"])
        self.dlg.presets.setCurrentIndex(-1)
        self.dlg._push()
        return True

    def _export(self) -> None:
        start = os.path.join(self.dlg.win.cfg.get("vst_preset_dir") or "", self.dlg.values["plugin"].get("name", "") +
                             ".vstpreset")
        path, _f = QFileDialog.getSaveFileName(self, t("vst_export"), start, t("vst_preset_filter") + " (*.vstpreset)")
        if path:
            self.export_preset(path)

    def export_preset(self, path: str) -> bool:
        self.read_state()
        data = self._call(self.host.preset_data) if self.host is not None else None
        if not data:
            return False
        if not path.lower().endswith(".vstpreset"):
            path += ".vstpreset"
        with open(path, "wb") as f:
            f.write(data)
        self.dlg.win.cfg["vst_preset_dir"] = os.path.dirname(path)
        return True

    def close(self) -> None:
        h, self.host = self.host, None
        if h is not None:
            if not self.error:
                try:
                    h.editor(False)
                except Exception:                        # noqa: BLE001
                    pass
            h.close()


def _index(raw: float, n: int) -> int:
    return max(0, min(n - 1, round(raw * (n - 1)))) if n > 1 else 0


def _value_text(p: dict) -> str:
    return f"{p.get('text', '')} {p.get('unit', '')}".strip()


# ── the menu ──────────────────────────────────────────────────
def fill_menu(win, menu: QMenu) -> None:
    """Effects → VST3 plugins ▸: the plugins by their maker (the last used first), then «Scan» and «Settings»."""
    menu.clear()
    lib = scan.library(win.cfg)
    plugins = lib.plugins()
    recent = [k for k in win.cfg.get("vst3_recent", []) if any(p.key == k for p in plugins)]
    if not plugins:
        a = menu.addAction(t("vst_none"))
        a.setEnabled(False)
    else:
        by_key = {p.key: p for p in plugins}
        for k in recent[:5]:
            p = by_key[k]
            menu.addAction(style.icon("fa6s.plug"), p.name, lambda p=p: win.vst_dialog(p))
        if recent:
            menu.addSeparator()
        makers: dict = {}
        for p in plugins:
            makers.setdefault(p.maker or t("vst_maker_none"), []).append(p)
        for maker in sorted(makers, key=str.lower):
            sub = menu.addMenu(maker)
            for p in makers[maker]:
                a = sub.addAction(p.name, lambda p=p: win.vst_dialog(p))
                a.setToolTip(f"{p.category} · {channels_text(p.channels)}")
    menu.addSeparator()
    menu.addAction(style.icon("fa6s.magnifying-glass"), t("vst_scan"), win.vst_scan)
    menu.addAction(style.icon("fa6s.gear"), t("vst_settings"), lambda: win.settings(page="vst3"))


def remember(cfg: dict, p) -> None:
    rec = [k for k in cfg.get("vst3_recent", []) if k != p.key]
    cfg["vst3_recent"] = [p.key] + rec[:9]


# ── the settings page ─────────────────────────────────────────
class _Scan(QObject):
    step = Signal(int, int, str)
    done = Signal(int, int)


class Vst3Page(QWidget):
    """Settings → VST3: where to look, the plugins found, those that did not load (crashed, hung, refused)."""

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        v = QVBoxLayout(self)
        v.addWidget(QLabel(t("vst_folders")))
        row = QHBoxLayout()
        self.folders = QListWidget()
        self.folders.setMaximumHeight(110)
        for f in scan.folders(cfg):
            self.folders.addItem(f)
        row.addWidget(self.folders, 1)
        col = QVBoxLayout()
        b_add = QPushButton(t("vst_folder_add"))
        b_add.clicked.connect(self._add)
        b_del = QPushButton(t("vst_folder_del"))
        b_del.clicked.connect(self._del)
        b_def = QPushButton(t("vst_folder_default"))
        b_def.clicked.connect(self._defaults)
        for b in (b_add, b_del, b_def):
            col.addWidget(b)
        col.addStretch(1)
        row.addLayout(col)
        v.addLayout(row)
        row = QHBoxLayout()
        self.b_scan = QPushButton(style.icon("fa6s.magnifying-glass"), t("vst_scan"))
        self.b_scan.clicked.connect(lambda: self.start_scan(False))
        self.b_rescan = QPushButton(t("vst_rescan"))
        self.b_rescan.clicked.connect(lambda: self.start_scan(True))
        row.addWidget(self.b_scan)
        row.addWidget(self.b_rescan)
        self.state = QLabel("")
        self.state.setObjectName("hint")
        row.addWidget(self.state, 1)
        v.addLayout(row)
        self.list = QTreeWidget()
        self.list.setHeaderLabels([t("vst_col_name"), t("vst_col_maker"), t("vst_col_cat"), t("vst_col_ch")])
        self.list.setRootIsDecorated(False)
        self.list.setMinimumHeight(200)
        self.list.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            self.list.header().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        v.addWidget(self.list, 1)
        v.addWidget(QLabel(t("vst_failed")))
        row = QHBoxLayout()
        self.failed = QListWidget()
        self.failed.setMaximumHeight(90)
        self.failed.setSelectionMode(QAbstractItemView.ExtendedSelection)
        row.addWidget(self.failed, 1)
        self.b_retry = QPushButton(t("vst_retry"))
        self.b_retry.clicked.connect(self._retry)
        row.addWidget(self.b_retry, 0, Qt.AlignTop)
        v.addLayout(row)
        note = QLabel(t("vst_note"))
        note.setObjectName("hint")
        note.setWordWrap(True)
        v.addWidget(note)
        self._sig = _Scan()
        self._sig.step.connect(self._step)
        self._sig.done.connect(self._done)
        self.running = False
        self.fill()

    def folder_list(self) -> list[str]:
        return [self.folders.item(i).text() for i in range(self.folders.count())]

    def apply(self) -> None:
        got = self.folder_list()
        self.cfg["vst3_folders"] = [] if got == scan.default_folders() else got

    def _add(self) -> None:
        p = QFileDialog.getExistingDirectory(self, t("vst_folder_add"))
        if p and os.path.normpath(p) not in self.folder_list():
            self.folders.addItem(os.path.normpath(p))

    def _del(self) -> None:
        for it in self.folders.selectedItems():
            self.folders.takeItem(self.folders.row(it))

    def _defaults(self) -> None:
        self.folders.clear()
        for f in scan.default_folders():
            self.folders.addItem(f)

    def fill(self) -> None:
        lib = scan.library(self.cfg)
        self.list.clear()
        plugins = lib.plugins()
        for p in plugins:
            it = QTreeWidgetItem([p.name, p.maker, p.category, channels_text(p.channels)])
            it.setToolTip(0, p.path)
            self.list.addTopLevelItem(it)
        self.failed.clear()
        for f in lib.failed():
            reason = {"crashed": t("vst_r_crashed"), "hung": t("vst_r_hung")}.get(f.reason) or \
                (t("vst_r_bad") if "unsupported plugin format" in f.reason or "Unable to scan" in f.reason
                 else f.reason)
            it_text = f"{os.path.basename(f.path)} — {reason}"
            self.failed.addItem(it_text)
            self.failed.item(self.failed.count() - 1).setData(Qt.UserRole, f.path.split(" — ")[0])
            self.failed.item(self.failed.count() - 1).setToolTip(f"{f.path}\n{f.reason} {f.detail}".strip())
        self.b_retry.setEnabled(self.failed.count() > 0 and not self.running)
        if not self.running:
            self.state.setText(t("vst_found", len(plugins), self.failed.count()))

    def start_scan(self, force: bool, retry: list[str] | None = None) -> None:
        if self.running:
            return
        self.apply()
        self.running = True
        for b in (self.b_scan, self.b_rescan, self.b_retry):
            b.setEnabled(False)
        self.state.setText(t("vst_scanning", 0, 0, ""))
        lib = scan.library(self.cfg)

        def work():
            try:
                n, bad = lib.scan(force=force, retry=retry,
                                  progress=lambda i, n, f: self._sig.step.emit(i, n, os.path.basename(f)))
            except OSError:
                n, bad = len(lib.plugins()), len(lib.failed())
            self._sig.done.emit(n, bad)
        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def wait(self, timeout: float = 300.0) -> None:
        """Tests: until the scan ends."""
        import time
        from PySide6.QtWidgets import QApplication
        end = time.monotonic() + timeout
        while self.running and time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.02)

    def _step(self, i: int, n: int, name: str) -> None:
        self.state.setText(t("vst_scanning", i, n, name))

    def _done(self, n: int, bad: int) -> None:
        self.running = False
        for b in (self.b_scan, self.b_rescan):
            b.setEnabled(True)
        config.save(self.cfg)
        self.fill()

    def _retry(self) -> None:
        paths = [it.data(Qt.UserRole) for it in self.failed.selectedItems()] or \
                [self.failed.item(i).data(Qt.UserRole) for i in range(self.failed.count())]
        if paths:
            self.start_scan(False, retry=paths)
