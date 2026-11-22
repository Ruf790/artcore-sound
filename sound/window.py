"""The main window: menus, the tool bar, a tab per open file, the overview strip, the waveform with the level meters
beside it, the transport (play cursor counter, selection fields, format) and the status bar. Long jobs (opening,
saving, effects) run in a thread with a progress bar and Cancel in the status bar."""
from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QIcon, QKeySequence
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QProgressBar, QScrollBar, QSplitter, QTabBar,
                               QToolButton, QVBoxLayout, QWidget)

from . import APP_TITLE, AUDIO_EXT, VERSION, VIDEO_EXT, config, ff, media, proc, style, winint
from .doc import Document, fmt_time, parse_time
from .i18n import fmt_bytes, t
from .player import Player
from .waveview import Meters, Overview, WaveView

UNDO_KEYS = {"open", "new", "cut", "paste", "delete", "silence", "insert silence", "trim"}


class Bridge(QObject):
    progress = Signal(float)
    done = Signal(object, object, object)       # result, error (str / None / "cancel"), the job's on_done
    update = Signal(object, str)


class MainWindow(QMainWindow):
    UPDATE_EVERY = 24 * 3600

    def __init__(self, cfg: dict, session):
        super().__init__()
        self.cfg, self.session = cfg, session
        self.docs: list[Document] = []
        self.clip = None
        self.player = Player(cfg.get("device", ""), cfg.get("latency", "low"))
        self.bridge = Bridge()
        self.bridge.progress.connect(self._on_progress)
        self.bridge.done.connect(self._on_job_done)
        self.bridge.update.connect(self._on_update_result)
        self.job = None                         # (label, Progress) while a long job runs
        self._job_doc = None
        self._open_queue: list = []
        self._untitled = 0
        self.update_info = None
        self._installer = ""
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(style.app_icon())
        self.setAcceptDrops(True)
        self.resize(1400, 860)
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        geo = cfg.get("geometry")
        if geo:
            from PySide6.QtCore import QByteArray
            self.restoreGeometry(QByteArray.fromHex(geo.encode()))
        self._update_all()

    # ── building ──────────────────────────────────────────────
    def _act(self, menu, icon: str, text: str, slot, keys=None) -> QAction:
        a = QAction(style.icon(icon) if icon else QIcon(), text, self)
        a.triggered.connect(lambda _c=False: slot())
        if keys:
            seqs = [QKeySequence(k) for k in (keys if isinstance(keys, (list, tuple)) else [keys])]
            a.setShortcuts(seqs)
            a.setShortcutContext(Qt.WindowShortcut)
        if menu is not None:
            menu.addAction(a)
        self.addAction(a)
        return a

    def _build(self) -> None:
        self.wave = WaveView()
        self.overview = Overview(self.wave)
        self.setMenuBar(self._build_menu())
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_toolbar())
        self.tabs = QTabBar()
        self.tabs.setObjectName("plTabs")
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setExpanding(False)
        self.tabs.setDocumentMode(True)
        self.tabs.setElideMode(Qt.ElideMiddle)
        self.tabs.currentChanged.connect(self._on_tab)
        self.tabs.tabCloseRequested.connect(self.close_doc)
        self.tabs.tabMoved.connect(self._on_tab_moved)
        self.tabs.hide()
        root.addWidget(self.tabs)
        root.addWidget(self.overview)
        self.overview.hide()
        mid = QSplitter(Qt.Horizontal)
        mid.setChildrenCollapsible(False)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        lv.addWidget(self.wave, 1)
        self.hbar = QScrollBar(Qt.Horizontal)
        lv.addWidget(self.hbar)
        mid.addWidget(left)
        right = QWidget()
        right.setObjectName("sidePanel")
        rv = QVBoxLayout(right)
        rv.setContentsMargins(6, 6, 6, 6)
        rv.setSpacing(4)
        cap = QLabel(t("meters"))
        cap.setObjectName("caption")
        rv.addWidget(cap)
        self.meters = Meters()
        rv.addWidget(self.meters, 1)
        mid.addWidget(right)
        mid.setStretchFactor(0, 1)
        mid.setStretchFactor(1, 0)
        mid.setSizes([1200, 170])
        self.splitter = mid
        root.addWidget(mid, 1)
        root.addWidget(self._build_transport())
        self.setCentralWidget(central)
        self._build_status()
        self.wave.selectionChanged.connect(self._on_selection)
        self.wave.viewChanged.connect(self._on_view)
        self.wave.seekRequested.connect(self._on_seek)
        self.wave.contextRequested.connect(self._context_menu)
        self.wave.filesDropped.connect(self.open_paths)
        self.overview.scrollRequested.connect(lambda f: self.wave.scroll_to(f - (self.wave.visible()[1] -
                                                                                   self.wave.visible()[0]) / 2))
        self.hbar.valueChanged.connect(self._on_hbar)
        self._hbar_unit = 1.0
        self._hbar_lock = False

    def _build_menu(self):
        from PySide6.QtWidgets import QMenuBar
        from . import updater
        mb = QMenuBar(self)
        A = self._act                                                     # noqa: N806
        m = mb.addMenu(APP_TITLE)
        A(m, "fa6s.gear", t("settings") + "…", self.settings_dialog, "Ctrl+,")
        if updater.can_self_update():
            A(m, "fa6s.cloud-arrow-down", t("upd_check_now"), lambda: self.check_updates(True))
        A(m, "fa6s.circle-info", t("about"), self.about)
        m.addSeparator()
        A(m, "", t("quit"), self.close, "Ctrl+Q")
        m = mb.addMenu(t("m_file"))
        A(m, "fa6s.folder-open", t("open") + "…", self.open_dialog, "Ctrl+O")
        self.recent_menu = m.addMenu(style.icon("fa6s.clock-rotate-left"), t("recent"))
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        m.addSeparator()
        self.a_save = A(m, "fa6s.floppy-disk", t("save"), self.save, "Ctrl+S")
        self.a_save_as = A(m, "fa6s.file-export", t("save_as") + "…", self.save_as, "Ctrl+Shift+S")
        self.a_save_sel = A(m, "fa6s.scissors", t("save_sel") + "…", lambda: self.save_as(selection=True))
        m.addSeparator()
        self.a_show = A(m, "fa6s.folder", t("show_file"), self._show_file)
        m.addSeparator()
        self.a_close = A(m, "fa6s.xmark", t("close"), lambda: self.close_doc(self.tabs.currentIndex()), "Ctrl+W")
        self.a_close_all = A(m, "", t("close_all"), self.close_all, "Ctrl+Shift+W")
        m = mb.addMenu(t("m_edit"))
        self.a_undo = A(m, "fa6s.rotate-left", t("undo"), self.undo, "Ctrl+Z")
        self.a_redo = A(m, "fa6s.rotate-right", t("redo"), self.redo, ["Ctrl+Y", "Ctrl+Shift+Z"])
        self.history_menu = m.addMenu(style.icon("fa6s.list"), t("history"))
        self.history_menu.aboutToShow.connect(self._fill_history)
        m.addSeparator()
        self.a_cut = A(m, "fa6s.scissors", t("cut"), self.cut, "Ctrl+X")
        self.a_copy = A(m, "fa6s.copy", t("copy"), self.copy, "Ctrl+C")
        self.a_paste = A(m, "fa6s.paste", t("paste"), self.paste, "Ctrl+V")
        self.a_paste_new = A(m, "fa6s.file-circle-plus", t("paste_new"), self.paste_new, "Ctrl+E")
        self.a_delete = A(m, "fa6s.trash-can", t("delete"), self.delete, "Delete")
        m.addSeparator()
        self.a_trim = A(m, "fa6s.crop-simple", t("trim"), self.trim, "Ctrl+T")
        self.a_silence = A(m, "fa6s.volume-xmark", t("silence"), self.silence, "Ctrl+L")
        self.a_ins_sil = A(m, "fa6s.arrows-left-right-to-line", t("insert_silence") + "…", self.insert_silence_dialog)
        m.addSeparator()
        self.a_sel_all = A(m, "", t("select_all"), self.select_all, "Ctrl+A")
        self.a_sel_none = A(m, "", t("select_none"), self.select_none, "Esc")
        self.a_sel_start = A(m, "", t("select_to_start"), lambda: self._select_to(0), "Shift+Home")
        self.a_sel_end = A(m, "", t("select_to_end"), lambda: self._select_to(None), "Shift+End")
        m = mb.addMenu(t("m_view"))
        self.a_zoom_in = A(m, "fa6s.magnifying-glass-plus", t("zoom_in"), lambda: self.wave.zoom(0.5), ["Up", "Ctrl+="])
        self.a_zoom_out = A(m, "fa6s.magnifying-glass-minus", t("zoom_out"), lambda: self.wave.zoom(2.0),
                            ["Down", "Ctrl+-"])
        self.a_zoom_all = A(m, "fa6s.expand", t("zoom_all"), self.wave.zoom_all, "Ctrl+Up")
        self.a_zoom_sel = A(m, "fa6s.arrows-left-right", t("zoom_sel"), self._zoom_sel, "Ctrl+Down")
        self.a_zoom_smp = A(m, "fa6s.wave-square", t("zoom_samples"), self.wave.zoom_samples, "Ctrl+Shift+Up")
        m.addSeparator()
        self.a_follow = A(m, "", t("follow"), self._toggle_follow)
        self.a_follow.setCheckable(True)
        self.a_follow.setChecked(self.cfg.get("follow", True))
        m.addSeparator()
        A(m, "fa6s.backward-step", t("go_start"), self.go_start, "Home")
        A(m, "fa6s.forward-step", t("go_end"), self.go_end, "End")
        A(m, "", t("rewind"), lambda: self.nudge(-1), "Left")
        A(m, "", t("forward"), lambda: self.nudge(1), "Right")
        m.addSeparator()
        tm = m.addMenu(t("theme"))
        grp = QActionGroup(self)
        for k in ("dark", "light", "auto"):
            a = tm.addAction(t(f"theme_{k}"))
            a.setCheckable(True)
            a.setChecked(self.cfg.get("theme") == k)
            a.triggered.connect(lambda _c=False, k=k: self._set_theme(k))
            grp.addAction(a)
        m = mb.addMenu(t("m_process"))
        self.proc_actions = []
        for icon, key, slot in (("fa6s.volume-high", "p_gain", self.gain_dialog),
                                ("fa6s.chart-simple", "p_normalize", self.normalize_dialog),
                                (None, None, None),
                                ("fa6s.arrow-trend-up", "p_fade_in", lambda: self.fade("fade_in")),
                                ("fa6s.arrow-trend-down", "p_fade_out", lambda: self.fade("fade_out")),
                                (None, None, None),
                                ("fa6s.right-left", "p_reverse", lambda: self.simple_op("reverse")),
                                ("fa6s.arrow-down-up-across-line", "p_invert", lambda: self.simple_op("invert")),
                                (None, None, None),
                                ("fa6s.gauge", "p_resample", self.resample_dialog),
                                ("fa6s.code-branch", "p_channels", self.channels_dialog)):
            if key is None:
                m.addSeparator()
                continue
            dots = "…" if key in ("p_gain", "p_normalize", "p_resample", "p_channels") else ""
            self.proc_actions.append(A(m, icon, t(key) + dots, slot))
        m = mb.addMenu(t("m_transport"))
        self.a_play = A(m, "fa6s.play", t("play"), self.play_toggle, "Space")
        self.a_play_all = A(m, "fa6s.circle-play", t("play_all"), self.play_all, "Shift+Space")
        A(m, "fa6s.pause", t("pause"), self.pause, "Ctrl+Space")
        A(m, "fa6s.stop", t("stop"), self.stop_play)
        self.a_loop = A(m, "fa6s.repeat", t("loop"), self._toggle_loop, "Q")
        self.a_loop.setCheckable(True)
        self.a_loop.setChecked(self.cfg.get("loop", False))
        return mb

    def _tool(self, icon: str, tip: str, slot, text: str = "", checkable: bool = False) -> QToolButton:
        b = QToolButton()
        b.setObjectName("ctlText" if text else "ctl")
        b.setIcon(style.icon(icon))
        b.setToolTip(tip)
        if text:
            b.setText(text)
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        b.setCheckable(checkable)
        b.clicked.connect(lambda _c=False: slot())
        return b

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("toolbar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(6, 4, 6, 4)
        h.setSpacing(2)
        T = self._tool                                                    # noqa: N806
        self.tb = {}
        for key, icon, tip, slot in (("open", "fa6s.folder-open", t("open_tip"), self.open_dialog),
                                     ("save", "fa6s.floppy-disk", t("save_tip"), self.save),
                                     ("|", None, None, None),
                                     ("undo", "fa6s.rotate-left", t("undo") + " (Ctrl+Z)", self.undo),
                                     ("redo", "fa6s.rotate-right", t("redo") + " (Ctrl+Y)", self.redo),
                                     ("|", None, None, None),
                                     ("cut", "fa6s.scissors", t("cut") + " (Ctrl+X)", self.cut),
                                     ("copy", "fa6s.copy", t("copy") + " (Ctrl+C)", self.copy),
                                     ("paste", "fa6s.paste", t("paste") + " (Ctrl+V)", self.paste),
                                     ("delete", "fa6s.trash-can", t("delete") + " (Del)", self.delete),
                                     ("|", None, None, None),
                                     ("trim", "fa6s.crop-simple", t("trim") + " (Ctrl+T)", self.trim),
                                     ("silence", "fa6s.volume-xmark", t("silence") + " (Ctrl+L)", self.silence),
                                     ("|", None, None, None),
                                     ("zoom_in", "fa6s.magnifying-glass-plus", t("zoom_in") + " (↑)",
                                      lambda: self.wave.zoom(0.5)),
                                     ("zoom_out", "fa6s.magnifying-glass-minus", t("zoom_out") + " (↓)",
                                      lambda: self.wave.zoom(2.0)),
                                     ("zoom_all", "fa6s.expand", t("zoom_all") + " (Ctrl+↑)", self.wave.zoom_all),
                                     ("|", None, None, None)):
            if key == "|":
                h.addWidget(self._sep())
                continue
            self.tb[key] = T(icon, tip, slot)
            h.addWidget(self.tb[key])
        for key, icon, text, slot in (("normalize", "fa6s.chart-simple", t("p_normalize"), self.normalize_dialog),
                                      ("fade_in", "fa6s.arrow-trend-up", t("p_fade_in"), lambda: self.fade("fade_in")),
                                      ("fade_out", "fa6s.arrow-trend-down", t("p_fade_out"),
                                       lambda: self.fade("fade_out"))):
            self.tb[key] = T(icon, text, slot, text)
            h.addWidget(self.tb[key])
        h.addStretch(1)
        return bar

    def _sep(self) -> QWidget:
        s = QWidget()
        s.setFixedSize(9, 22)
        s.setObjectName("tbSep")
        return s

    def _build_transport(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("transport")
        h = QHBoxLayout(bar)
        h.setContentsMargins(10, 6, 10, 6)
        h.setSpacing(4)
        T = self._tool                                                    # noqa: N806
        self.t_start = T("fa6s.backward-step", t("go_start") + " (Home)", self.go_start)
        self.t_rew = T("fa6s.backward", t("rewind") + " (←)", lambda: self.nudge(-1))
        self.t_play = T("fa6s.play", t("play_tip"), self.play_toggle)
        self.t_play.setObjectName("play")
        self.t_play.setIconSize(self.t_play.iconSize() * 1.25)
        self.t_pause = T("fa6s.pause", t("pause") + " (Ctrl+Space)", self.pause)
        self.t_stop = T("fa6s.stop", t("stop"), self.stop_play)
        self.t_ff = T("fa6s.forward", t("forward") + " (→)", lambda: self.nudge(1))
        self.t_end = T("fa6s.forward-step", t("go_end") + " (End)", self.go_end)
        self.t_loop = T("fa6s.repeat", t("loop") + " (Q)", self._toggle_loop, checkable=True)
        self.t_loop.setChecked(self.cfg.get("loop", False))
        self.t_rec = T("fa6s.circle", t("record"), lambda: None)
        self.t_rec.setIcon(style.icon("fa6s.circle", "#e5484d"))
        self.t_rec.setEnabled(False)
        for b in (self.t_start, self.t_rew, self.t_play, self.t_pause, self.t_stop, self.t_ff, self.t_end,
                  self.t_loop, self.t_rec):
            h.addWidget(b)
        h.addSpacing(14)
        self.counter = QLabel(fmt_time(0, 48000))
        self.counter.setObjectName("counter")
        self.counter.setMinimumWidth(190)
        h.addWidget(self.counter)
        h.addSpacing(14)
        self.f_start, self.f_end, self.f_len = (self._time_field(k) for k in ("sel_start", "sel_end", "sel_len"))
        for key, field in (("sel_start", self.f_start), ("sel_end", self.f_end), ("sel_len", self.f_len)):
            box = QVBoxLayout()
            box.setSpacing(0)
            cap = QLabel(t(key))
            cap.setObjectName("caption")
            box.addWidget(cap)
            box.addWidget(field)
            h.addLayout(box)
        h.addStretch(1)
        self.fmt_label = QLabel("")
        self.fmt_label.setObjectName("format")
        h.addWidget(self.fmt_label)
        return bar

    def _time_field(self, key: str) -> QLineEdit:
        e = QLineEdit()
        e.setObjectName("timeField")
        e.setFixedWidth(112)
        e.setAlignment(Qt.AlignCenter)
        e.setPlaceholderText(t("sel_none"))
        e.editingFinished.connect(lambda k=key, e=e: self._on_time_edit(k, e))
        return e

    def _build_status(self) -> None:
        sb = self.statusBar()
        self.job_label = QLabel("")
        self.job_bar = QProgressBar()
        self.job_bar.setFixedWidth(220)
        self.job_bar.setRange(0, 1000)
        self.job_cancel = QToolButton()
        self.job_cancel.setObjectName("ctlText")
        self.job_cancel.setText(t("cancel"))
        self.job_cancel.clicked.connect(self.cancel_job)
        for w in (self.job_label, self.job_bar, self.job_cancel):
            sb.addPermanentWidget(w)
            w.hide()
        self.st_sel = QLabel("")
        self.st_disk = QLabel("")
        self.st_dev = QLabel("")
        for w in (self.st_sel, self.st_disk, self.st_dev):
            w.setObjectName("hint")
            sb.addPermanentWidget(w)

    # ── documents ─────────────────────────────────────────────
    @property
    def doc(self) -> Document | None:
        i = self.tabs.currentIndex()
        return self.docs[i] if 0 <= i < len(self.docs) else None

    def add_doc(self, d: Document) -> None:
        self.docs.append(d)
        i = self.tabs.addTab(style.icon("fa6s.wave-square"), d.name)
        self.tabs.setTabToolTip(i, d.path or d.name)
        self.tabs.show()
        self.tabs.setCurrentIndex(i)
        self._on_tab(i)

    def _on_tab(self, i: int) -> None:
        self.stop_play()
        d = self.doc
        self.wave.set_doc(d)
        self.overview.setVisible(d is not None)
        self.meters.set_channels(d.labels if d else ["L", "R"])
        self.meters.reset()
        self._update_all()

    def _on_tab_moved(self, a: int, b: int) -> None:
        self.docs.insert(b, self.docs.pop(a))

    def close_doc(self, i: int) -> bool:
        if not (0 <= i < len(self.docs)):
            return False
        d = self.docs[i]
        if self.job and self._job_doc is d:
            self._toast(t("busy", self.job[0]))
            return False
        if d.dirty and not self._ask_save(d, i):
            return False
        self.stop_play()
        self.docs.pop(i)
        self.tabs.blockSignals(True)
        self.tabs.removeTab(i)
        self.tabs.blockSignals(False)
        d.close()
        if not self.docs:
            self.tabs.hide()
        self._on_tab(self.tabs.currentIndex())
        return True

    def close_all(self) -> bool:
        while self.docs:
            if not self.close_doc(len(self.docs) - 1):
                return False
        return True

    def _ask_save(self, d: Document, i: int) -> bool:
        """«Save the changes?» → True to go on (saved or not), False to stay."""
        self.tabs.setCurrentIndex(i)
        box = QMessageBox(QMessageBox.Question, APP_TITLE, t("ask_save", d.name), parent=self)
        save = box.addButton(t("save"), QMessageBox.AcceptRole)
        nosave = box.addButton(t("dont_save"), QMessageBox.DestructiveRole)
        box.addButton(t("cancel"), QMessageBox.RejectRole)
        box.setDefaultButton(save)
        box.exec()
        b = box.clickedButton()
        if b is nosave:
            return True
        if b is save:
            return self.save(wait=True)
        return False

    def _title(self) -> None:
        d = self.doc
        self.setWindowTitle(f"{d.name}{' •' if d.dirty else ''} — {APP_TITLE}" if d else APP_TITLE)
        for i, x in enumerate(self.docs):
            self.tabs.setTabText(i, x.name + (" •" if x.dirty else ""))

    # ── opening ───────────────────────────────────────────────
    def open_dialog(self) -> None:
        exts = " ".join(f"*{e}" for e in AUDIO_EXT + VIDEO_EXT)
        paths, _ = QFileDialog.getOpenFileNames(self, t("open"), self.cfg.get("last_dir", ""),
                                                f"{t('files_audio')} ({exts});;{t('files_all')} (*)")
        if paths:
            self.cfg["last_dir"] = os.path.dirname(paths[0])
            self.open_paths(paths)

    def open_paths(self, paths: list[str]) -> None:
        files = []
        for p in paths:
            if os.path.isdir(p):
                for name in sorted(os.listdir(p)):
                    if name.lower().endswith(AUDIO_EXT):
                        files.append(os.path.join(p, name))
            elif os.path.isfile(p):
                files.append(p)
        self._open_queue += files
        self._open_next()

    def _open_next(self) -> None:
        if self.job or not self._open_queue:
            return
        path = self._open_queue.pop(0)
        for i, d in enumerate(self.docs):
            if d.path and os.path.normcase(os.path.abspath(d.path)) == os.path.normcase(os.path.abspath(path)):
                self.tabs.setCurrentIndex(i)
                self._toast(t("already_open", d.name))
                QTimer.singleShot(0, self._open_next)
                return
        if not ff.ffmpeg() or not ff.ffprobe():
            QMessageBox.critical(self, APP_TITLE, t("no_ffmpeg"))
            self._open_queue.clear()
            return
        try:
            info = ff.probe(path)
        except Exception as e:
            QMessageBox.warning(self, APP_TITLE, t("open_failed", os.path.basename(path), e))
            QTimer.singleShot(0, self._open_next)
            return
        pos = info.tracks[0].pos
        if len(info.tracks) > 1:
            from .dialogs import TrackDialog
            dlg = TrackDialog(info, self)
            if not dlg.exec():
                QTimer.singleShot(0, self._open_next)
                return
            pos = dlg.track_pos()
        track = next(tr for tr in info.tracks if tr.pos == pos)
        need = media.needed_bytes(track)
        free = winint.free_space(self.session.path)
        if 0 <= free < need * 1.05 and QMessageBox.question(
                self, APP_TITLE, t("no_space", os.path.basename(path), fmt_bytes(need), self.session.path,
                                   fmt_bytes(free))) != QMessageBox.Yes:
            QTimer.singleShot(0, self._open_next)
            return
        folder = self.session.folder()
        config.add_recent(self.cfg, path)
        config.save(self.cfg)

        def work(progress):
            return media.open_file(path, folder, pos, progress, info)

        def done(d, err):
            if err:
                from .store import remove_folder
                remove_folder(folder)
                if err != "cancel":
                    QMessageBox.warning(self, APP_TITLE, t("open_failed", os.path.basename(path), err))
            else:
                self.add_doc(d)
            QTimer.singleShot(0, self._open_next)
        self.run_job(t("opening", os.path.basename(path)), work, done, None)

    def _fill_recent(self) -> None:
        m = self.recent_menu
        m.clear()
        rec = [p for p in self.cfg.get("recent", []) if os.path.isfile(p)]
        if not rec:
            a = m.addAction(t("recent_none"))
            a.setEnabled(False)
            return
        for p in rec:
            a = m.addAction(os.path.basename(p))
            a.setToolTip(p)
            a.triggered.connect(lambda _c=False, p=p: self.open_paths([p]))
        m.addSeparator()
        m.addAction(t("recent_clear")).triggered.connect(lambda: (self.cfg.update(recent=[]), config.save(self.cfg)))

    # ── saving ────────────────────────────────────────────────
    def save(self, wait: bool = False) -> bool:
        d = self.doc
        if d is None or self._busy():
            return False
        if not d.save_format or not d.path:
            return self.save_as(wait=wait)
        return self._save_to(d, d.path, d.save_format, d.save_opts.get("option", ""), wait=wait)

    def save_as(self, selection: bool = False, wait: bool = False) -> bool:
        d = self.doc
        if d is None or self._busy():
            return False
        from .dialogs import SaveDialog
        fmt = d.save_format or self.cfg.get("save_format") or "wav"
        if fmt not in media.FORMATS:
            fmt = "wav"
        folder = os.path.dirname(d.path) if d.path else (self.cfg.get("save_dir") or os.path.expanduser("~"))
        stem = os.path.splitext(d.name)[0] if d.name else "sound"
        opt = d.save_opts.get("option", "") if d.save_format == fmt else \
            self.cfg.get("save_opts", {}).get(fmt, "")
        dlg = SaveDialog(d, os.path.join(folder, stem + media.FORMATS[fmt].ext), fmt, opt, self, selection)
        if not dlg.exec():
            return False
        path, fmt, opt = dlg.path(), dlg.format(), dlg.option()
        if not path:
            return False
        if os.path.exists(path) and os.path.normcase(os.path.abspath(path)) != \
                os.path.normcase(os.path.abspath(d.path or "")) and \
                QMessageBox.question(self, APP_TITLE, t("sv_exists", os.path.basename(path))) != QMessageBox.Yes:
            return False
        self.cfg["save_format"] = fmt
        self.cfg.setdefault("save_opts", {})[fmt] = opt
        self.cfg["save_dir"] = os.path.dirname(path)
        config.save(self.cfg)
        rng = d.sel if dlg.only_selection() and d.sel else None
        return self._save_to(d, path, fmt, opt, rng, wait=wait)

    def _save_to(self, d: Document, path: str, fmt: str, opt: str, rng=None, wait: bool = False) -> bool:
        self.stop_play()
        a, b = rng if rng else (0, d.frames)
        result = {}

        def work(progress):
            media.save(d, path, fmt, opt, progress, a, b)
            return path

        def done(_res, err):
            if err:
                result["ok"] = False
                if err != "cancel":
                    QMessageBox.warning(self, APP_TITLE, t("save_failed", os.path.basename(path), err))
                return
            result["ok"] = True
            if rng is None:                        # the whole file: the document is now that file
                d.path, d.name, d.save_format, d.save_opts = path, os.path.basename(path), fmt, {"option": opt}
                d.mark_saved()
                config.add_recent(self.cfg, path)
                config.save(self.cfg)
            self._toast(t("saved", path))
            self._update_all()
        self.run_job(t("saving", os.path.basename(path)), work, done, d)
        if wait:
            while self.job is not None:
                QApplication.processEvents()
                time.sleep(0.01)
            return bool(result.get("ok"))
        return True

    def _show_file(self) -> None:
        d = self.doc
        if d and d.path:
            winint.show_in_explorer(d.path)

    # ── long jobs ─────────────────────────────────────────────
    def run_job(self, label: str, work, on_done, d: Document | None) -> None:
        """Run work(progress) in a thread; on_done(result, error) back in the window. One job at a time."""
        progress = media.Progress(lambda f: self.bridge.progress.emit(f))
        self.job = (label, progress)
        self._job_doc = d
        self.job_label.setText(label)
        self.job_bar.setValue(0)
        for w in (self.job_label, self.job_bar, self.job_cancel):
            w.show()
        self._update_actions()

        def go():
            try:
                res = work(progress)
                self.bridge.done.emit(res, None, on_done)
            except media.Cancelled:
                self.bridge.done.emit(None, "cancel", on_done)
            except Exception as e:
                self.bridge.done.emit(None, str(e) or type(e).__name__, on_done)
        threading.Thread(target=go, daemon=True).start()

    def _on_progress(self, f: float) -> None:
        self.job_bar.setValue(int(f * 1000))

    def _on_job_done(self, res, err, on_done) -> None:
        self.job = None
        self._job_doc = None
        for w in (self.job_label, self.job_bar, self.job_cancel):
            w.hide()
        if err == "cancel":
            self._toast(t("cancelled"))
        try:
            on_done(res, err)
        finally:
            self._update_all()

    def cancel_job(self) -> None:
        if self.job:
            self.job[1].cancel()

    def _busy(self) -> bool:
        if self.job:
            self._toast(t("busy", self.job[0]))
            return True
        return False

    # ── editing ───────────────────────────────────────────────
    def _edit_doc(self) -> Document | None:
        d = self.doc
        if d is None or self._busy():
            return None
        self.stop_play()
        return d

    def _edited(self) -> None:
        d = self.doc
        if d is not None:
            self.meters.set_channels(d.labels)            # undo of a channel change changes the meters too
        self.wave.refresh()
        self.overview.update()
        self._update_all()

    def _range(self, d: Document, whole_if_none: bool = True):
        if d.sel:
            return d.sel
        return (0, d.frames) if whole_if_none else None

    def undo(self) -> None:
        d = self._edit_doc()
        if d and d.undo():
            self._edited()

    def redo(self) -> None:
        d = self._edit_doc()
        if d and d.redo():
            self._edited()

    def _fill_history(self) -> None:
        m = self.history_menu
        m.clear()
        d = self.doc
        if d is None:
            return
        for i, st in enumerate(d.history):
            a = m.addAction(f"{i + 1}. {self._label(st.label)}")
            a.setCheckable(True)
            a.setChecked(i == d.pos)
            a.triggered.connect(lambda _c=False, i=i: self._goto_state(i))

    def _goto_state(self, i: int) -> None:
        d = self._edit_doc()
        if d:
            d.goto_state(i)
            self._edited()

    @staticmethod
    def _label(key: str) -> str:
        return t("u_" + key.replace(" ", "_")) if key in UNDO_KEYS else key

    def cut(self) -> None:
        d = self._edit_doc()
        if d and d.sel:
            self.clip = d.cut(*d.sel)
            self._edited()

    def copy(self) -> None:
        d = self.doc
        if d and d.sel:
            self.clip = d.copy(*d.sel)
            self._update_actions()

    def delete(self) -> None:
        d = self._edit_doc()
        if d and d.sel:
            d.delete(*d.sel)
            self._edited()

    def trim(self) -> None:
        d = self._edit_doc()
        if d and d.sel:
            d.trim(*d.sel)
            self.wave.zoom_all()
            self._edited()

    def silence(self) -> None:
        d = self._edit_doc()
        if d and d.sel:
            d.silence(*d.sel)
            self._edited()

    def paste(self) -> None:
        d = self._edit_doc()
        if d is None or self.clip is None:
            return
        a, b = d.sel if d.sel else (d.cursor, d.cursor)
        if d.fits(self.clip):
            d.paste(self.clip, a, b)
            self._edited()
            return
        clip = self.clip
        self._toast(t("clip_other", clip.rate, clip.channels, d.rate, d.channels))

        def done(conv, err):
            if err:
                if err != "cancel":
                    QMessageBox.warning(self, APP_TITLE, t("op_failed", err))
                return
            d.paste(conv, a, b)
            self._edited()
        self.run_job(t("converting"), lambda pr: proc.convert_clip(clip, d, pr), done, d)

    def paste_new(self) -> None:
        if self.clip is None or self._busy():
            return
        from .doc import new_document
        c = self.clip
        self._untitled += 1
        d = new_document(self.session.folder(), c.rate, c.channels, c.layout, t("untitled", self._untitled))
        d.paste(c, 0)
        d.history = d.history[-1:]
        d.pos = 0
        d.history[0].label = "new"
        d.saved_id = -1
        d.sel = None
        self.add_doc(d)

    def insert_silence_dialog(self) -> None:
        d = self.doc
        if d is None or self._busy():
            return
        from .dialogs import ValueDialog
        dlg = ValueDialog(t("insert_silence"), self)
        f = dlg.time_field(t("length"), d.rate, d.rate)
        if dlg.exec():
            n = ValueDialog.frames_of(f)
            d = self._edit_doc()
            if d and n:
                d.insert_silence(d.cursor if not d.sel else d.sel[0], n)
                self._edited()

    def select_all(self) -> None:
        d = self.doc
        if d and d.frames:
            d.sel = (0, d.frames)
            self._on_selection()
            self.wave.update()

    def select_none(self) -> None:
        d = self.doc
        if d:
            d.sel = None
            self._on_selection()
            self.wave.update()

    def _select_to(self, end) -> None:
        d = self.doc
        if d is None:
            return
        target = d.frames if end is None else 0
        a, b = sorted((d.cursor, target))
        d.sel = (a, b) if b > a else None
        self._on_selection()
        self.wave.update()

    def _zoom_sel(self) -> None:
        d = self.doc
        if d and d.sel:
            self.wave.zoom_range(*d.sel)

    def _context_menu(self, pos: QPoint) -> None:
        m = QMenu(self)
        for a in (self.a_cut, self.a_copy, self.a_paste, self.a_delete, None, self.a_trim, self.a_silence,
                  self.a_ins_sil, None, self.a_sel_all, self.a_zoom_sel, self.a_zoom_all, None, *self.proc_actions[:4]):
            if a is None:
                m.addSeparator()
            else:
                m.addAction(a)
        m.exec(pos)

    # ── processing ────────────────────────────────────────────
    def _process(self, label: str, make, rng=None, meta=None) -> None:
        """make(progress) → a new source for rng (default: the selection or the whole file) in a thread."""
        d = self._edit_doc()
        if d is None or d.frames <= 0:
            return
        a, b = rng or self._range(d)
        whole = (a, b) == (0, d.frames)
        had_sel = d.sel

        def done(src, err):
            if err:
                if err != "cancel":
                    QMessageBox.warning(self, APP_TITLE, t("op_failed", err))
                return
            if meta:
                d.replace(0, d.frames, src, label, sel=None, **meta)
                self.meters.set_channels(d.labels)
                self.wave.zoom_all()
            else:
                d.replace(a, b, src, label, sel=had_sel if had_sel else None)
            self._edited()
        if whole and not had_sel and not meta:
            self._toast(t("p_whole"))
        self.run_job(t("processing", label), make, done, d)

    def simple_op(self, op: str, **kw) -> None:
        d = self.doc
        if d is None:
            return
        a, b = self._range(d)
        label = t({"reverse": "p_reverse", "invert": "p_invert", "fade_in": "p_fade_in", "fade_out": "p_fade_out",
                   "gain": "p_gain", "normalize": "p_normalize"}[op])
        self._process(label, lambda pr: proc.apply_op(d, op, a, b, pr, **kw), (a, b))

    def fade(self, op: str, curve: str = "linear") -> None:
        self.simple_op(op, curve=curve)

    def gain_dialog(self) -> None:
        d = self.doc
        if d is None or self._busy():
            return
        from .dialogs import ValueDialog
        dlg = ValueDialog(t("p_gain_title"), self, t("p_whole") if not d.sel else "")
        s = dlg.spin(t("p_gain_by"), 3.0, -96, 48, " dB")
        if dlg.exec():
            self.simple_op("gain", db=s.value())

    def normalize_dialog(self) -> None:
        d = self.doc
        if d is None or self._busy() or d.frames <= 0:
            return
        from .dialogs import ValueDialog
        a, b = self._range(d)
        pk = float(d.peak(a, b).max())
        if pk <= 0:
            QMessageBox.information(self, APP_TITLE, t("p_silent"))
            return
        now = f"{proc.gain_to_db(pk):.2f}"
        dlg = ValueDialog(t("p_norm_title"), self, t("p_norm_now", now) + ("" if d.sel else "\n" + t("p_whole")))
        s = dlg.spin(t("p_norm_to"), -0.1, -60, 0, " dBFS", 2, 0.1)
        if dlg.exec():
            self.simple_op("normalize", db=s.value())

    def resample_dialog(self) -> None:
        d = self.doc
        if d is None or self._busy() or d.frames <= 0:
            return
        from .dialogs import RATES, ValueDialog
        dlg = ValueDialog(t("rs_title"), self, t("rs_note"))
        c = dlg.combo(t("rs_rate"), [(f"{r} Hz", r) for r in RATES], 48000 if d.rate != 48000 else 44100)
        if dlg.exec() and c.currentData() != d.rate:
            rate = int(c.currentData())
            self._process(t("p_resample") + f" {rate} Hz", lambda pr: proc.convert_document(d, rate, d.channels,
                                                                                            d.layout, pr),
                          (0, d.frames), {"rate": rate})

    def channels_dialog(self) -> None:
        d = self.doc
        if d is None or self._busy() or d.frames <= 0:
            return
        from .dialogs import ValueDialog
        items = [(t("ch_mono"), 1), (t("ch_stereo"), 2), ("5.1", 6)]
        dlg = ValueDialog(t("chn_title"), self, t("chn_note"))
        c = dlg.combo(t("chn_to"), items, 2 if d.channels != 2 else 1)
        if dlg.exec() and c.currentData() != d.channels:
            ch = int(c.currentData())
            lay = ff.layout_for(ch)
            self._process(t("p_channels") + f": {lay}", lambda pr: proc.convert_document(d, d.rate, ch, lay, pr),
                          (0, d.frames), {"channels": ch, "layout": lay, "labels": tuple(ff.labels(ch, lay))})

    # ── transport ─────────────────────────────────────────────
    def play_toggle(self) -> None:
        if self.player.state == "play":
            self.stop_play()
        elif self.player.state == "pause":
            self.player.resume()
        else:
            self.play()

    def play(self) -> None:
        d = self.doc
        if d is None or d.frames <= 0 or self.job:
            return
        loop = self.a_loop.isChecked()
        if d.sel:
            a, b = d.sel
            if not (a <= d.cursor < b) or not loop:
                start = a
            else:
                start = d.cursor
            ok = self.player.play(d, start, b, loop)
            if ok and start != a:
                self.player.start = a                  # a loop starts again at the selection's start
        else:
            start = d.cursor if d.cursor < d.frames else 0
            ok = self.player.play(d, start, d.frames, loop)
        if not ok and self.player.error:
            self._toast(t("playback_err", self.player.error))
        self._update_transport()

    def play_all(self) -> None:
        d = self.doc
        if d is None or d.frames <= 0 or self.job:
            return
        self.player.play(d, 0, d.frames, self.a_loop.isChecked())
        self._update_transport()

    def pause(self) -> None:
        if self.player.state == "play":
            self.player.pause()
        elif self.player.state == "pause":
            self.player.resume()
        self._update_transport()

    def stop_play(self) -> None:
        if self.player.state == "stop":
            return
        d = self.player.doc
        pos = self.player.position()
        origin = self.player.origin
        self.player.stop()
        if d is not None and d is self.doc:
            d.cursor = origin if self.cfg.get("return_cursor", True) else pos
        self.wave.set_play_pos(None)
        self.meters.feed(None)
        self._update_transport()
        self._on_selection()

    def _on_seek(self, frame: int) -> None:
        """A click in the wave while playing: go on from there."""
        if self.player.state == "play" and self.player.doc is self.doc:
            d = self.doc
            end = d.sel[1] if d.sel and d.sel[0] <= frame < d.sel[1] else d.frames
            self.player.play(d, frame, end, self.a_loop.isChecked())

    def go_start(self) -> None:
        d = self.doc
        if d:
            self.stop_play()
            d.cursor = 0
            self.wave.ensure_visible(0)
            self._on_selection()
            self.wave.update()

    def go_end(self) -> None:
        d = self.doc
        if d:
            self.stop_play()
            d.cursor = d.frames
            self.wave.ensure_visible(d.frames)
            self._on_selection()
            self.wave.update()

    def nudge(self, direction: int) -> None:
        """← / →: the cursor a tenth of the view (while playing: 5 seconds)."""
        d = self.doc
        if d is None:
            return
        if self.player.state == "play" and self.player.doc is d:
            pos = self.player.position() + direction * 5 * d.rate
            self._on_seek(int(max(0, min(d.frames - 1, pos))))
            return
        a, b = self.wave.visible()
        d.cursor = int(max(0, min(d.frames, d.cursor + direction * max(1, (b - a) / 10))))
        self.wave.ensure_visible(d.cursor)
        self._on_selection()
        self.wave.update()

    def _toggle_loop(self) -> None:
        on = not self.cfg.get("loop", False)
        self.cfg["loop"] = on
        self.a_loop.setChecked(on)
        self.t_loop.setChecked(on)
        self.player.loop = on

    def _toggle_follow(self) -> None:
        self.cfg["follow"] = self.a_follow.isChecked()

    def _tick(self) -> None:
        p = self.player
        if p.state == "play":
            if p.done:
                self.stop_play()
                return
            pos = p.position()
            if p.doc is self.doc:
                self.wave.set_play_pos(pos, self.cfg.get("follow", True))
                self.overview.update()
                self.counter.setText(fmt_time(pos, p.doc.rate))
            self.meters.feed(p.levels())
        elif self.meters.level.max() > self.meters.FLOOR:
            self.meters.feed(None)

    # ── keeping the widgets in step ───────────────────────────
    def _on_selection(self) -> None:
        d = self.doc
        self._hbar_sync()
        self.overview.update()
        if d is None:
            for f in (self.f_start, self.f_end, self.f_len):
                f.setText("")
            self.counter.setText(fmt_time(0, 48000))
            self.st_sel.setText("")
            self._update_actions()
            return
        if self.player.state != "play":
            self.counter.setText(fmt_time(d.cursor, d.rate))
        if d.sel:
            a, b = d.sel
            self.f_start.setText(fmt_time(a, d.rate))
            self.f_end.setText(fmt_time(b, d.rate))
            self.f_len.setText(fmt_time(b - a, d.rate))
            self.st_sel.setText(t("st_sel", t("st_frames", f"{b - a:,}".replace(",", " "))))
        else:
            self.f_start.setText(fmt_time(d.cursor, d.rate))
            self.f_end.setText("")
            self.f_len.setText("")
            self.st_sel.setText(t("st_frames", f"{d.frames:,}".replace(",", " ")))
        self._update_actions()

    def _on_time_edit(self, key: str, e: QLineEdit) -> None:
        d = self.doc
        if d is None:
            return
        v = parse_time(e.text(), d.rate)
        if v is None:
            self._on_selection()
            return
        v = max(0, min(d.frames, v))
        a, b = d.sel if d.sel else (d.cursor, d.cursor)
        if key == "sel_start":
            if d.sel:
                a = min(v, b)
            else:
                a = b = v
                d.cursor = v
        elif key == "sel_end":
            b = max(v, a)
        else:
            b = min(d.frames, a + v)
        d.sel = (a, b) if b > a else None
        if d.sel:
            d.cursor = a
        self.wave.ensure_visible(a)
        self.wave.update()
        self._on_selection()

    def _on_view(self) -> None:
        self._hbar_sync()
        self.overview.update()

    def _hbar_sync(self) -> None:
        d = self.doc
        self._hbar_lock = True
        if d is None or d.frames <= 0:
            self.hbar.setRange(0, 0)
        else:
            a, b = self.wave.visible()
            span = b - a
            unit = max(1.0, self.wave.spp(), d.frames / 1e9)
            self._hbar_unit = unit
            self.hbar.setRange(0, int(max(0, d.frames - span) / unit))
            self.hbar.setPageStep(max(1, int(span / unit)))
            self.hbar.setSingleStep(max(1, int(span / unit / 20)))
            self.hbar.setValue(int(a / unit))
        self._hbar_lock = False

    def _on_hbar(self, v: int) -> None:
        if not self._hbar_lock:
            self.wave.scroll_to(v * self._hbar_unit)

    def _update_actions(self) -> None:
        d = self.doc
        busy = self.job is not None
        has = d is not None
        sel = has and bool(d.sel)
        for a, on in ((self.a_save, has), (self.a_save_as, has), (self.a_save_sel, sel), (self.a_close, has),
                      (self.a_close_all, has), (self.a_show, has and bool(d.path)),
                      (self.a_undo, has and d.can_undo()), (self.a_redo, has and d.can_redo()),
                      (self.a_cut, sel), (self.a_copy, sel), (self.a_delete, sel), (self.a_trim, sel),
                      (self.a_silence, sel), (self.a_paste, has and self.clip is not None),
                      (self.a_paste_new, self.clip is not None), (self.a_ins_sil, has), (self.a_sel_all, has),
                      (self.a_zoom_sel, sel), (self.a_play, has), (self.a_play_all, has)):
            a.setEnabled(on and not (busy and a not in (self.a_copy, self.a_close_all)))
        for a in self.proc_actions:
            a.setEnabled(has and not busy)
        self.a_undo.setText(t("undo_n", self._label(d.undo_label())) if has and d.can_undo() else t("undo"))
        self.a_redo.setText(t("redo_n", self._label(d.redo_label())) if has and d.can_redo() else t("redo"))
        for key, act in (("save", self.a_save), ("undo", self.a_undo), ("redo", self.a_redo), ("cut", self.a_cut),
                         ("copy", self.a_copy), ("paste", self.a_paste), ("delete", self.a_delete),
                         ("trim", self.a_trim), ("silence", self.a_silence)):
            self.tb[key].setEnabled(act.isEnabled())
        for key in ("zoom_in", "zoom_out", "zoom_all"):
            self.tb[key].setEnabled(has)
        for key in ("normalize", "fade_in", "fade_out"):
            self.tb[key].setEnabled(has and not busy)
        self.tb["undo"].setToolTip(self.a_undo.text() + " (Ctrl+Z)")
        self.tb["redo"].setToolTip(self.a_redo.text() + " (Ctrl+Y)")
        self._title()

    def _update_transport(self) -> None:
        st = self.player.state
        self.t_play.setIcon(style.icon("fa6s.stop" if st == "play" else "fa6s.play", "#ffffff"))
        has = self.doc is not None
        for b in (self.t_start, self.t_rew, self.t_play, self.t_pause, self.t_stop, self.t_ff, self.t_end):
            b.setEnabled(has)

    def _update_all(self) -> None:
        d = self.doc
        if d is None:
            self.fmt_label.setText("")
            self.fmt_label.hide()
        else:
            if d.track is not None and d.track.bits and not d.track.codec.startswith("pcm_f"):
                bits = t("fmt_bits", d.track.bits)
            elif d.track is not None and d.track.codec and not d.track.codec.startswith("pcm_"):
                bits = ff.codec_name(d.track)
            else:
                bits = t("fmt_float")
            chans = {1: t("ch_mono"), 2: t("ch_stereo")}.get(d.channels, (d.layout or t("ch_n", d.channels))
                                                              .replace("(side)", ""))
            self.fmt_label.setText(t("fmt_line", f"{d.rate:,}".replace(",", " "), bits, chans) + " · " +
                                   fmt_time(d.frames, d.rate))
            self.fmt_label.show()
        self.st_disk.setText(t("st_disk", fmt_bytes(sum(x.disk_bytes() for x in self.docs))) if self.docs else "")
        be = self.player.backend
        self.st_dev.setText(t("st_dev", t("st_null") if be == "null" else (self.cfg.get("device") or "WASAPI"))
                            if be else "")
        self._on_selection()
        self._update_transport()

    def _toast(self, text: str) -> None:
        self.statusBar().showMessage(text, 5000)

    # ── settings, about, updates ──────────────────────────────
    def settings_dialog(self) -> None:
        from .dialogs import SettingsDialog
        dlg = SettingsDialog(self.cfg, self)
        before = (self.cfg.get("language"), self.cfg.get("theme"), self.cfg.get("look"))
        if dlg.exec():
            dlg.apply()
            config.save(self.cfg)
            self.player.device_name = self.cfg.get("device", "")
            self.player.latency = self.cfg.get("latency", "low")
            if before != (self.cfg.get("language"), self.cfg.get("theme"), self.cfg.get("look")):
                QMessageBox.information(self, APP_TITLE, t("lang_restart"))

    def _set_theme(self, k: str) -> None:
        self.cfg["theme"] = k
        config.save(self.cfg)
        style.set_look(self.cfg.get("look", "classic"))
        style.set_theme(k)
        QApplication.instance().setStyleSheet(style.stylesheet())
        self.wave.refresh()
        self.overview.update()
        self.meters.update()
        self._update_transport()

    def about(self) -> None:
        from . import player as P
        devs = P.output_devices()
        dev = self.cfg.get("device") or next((x["name"] for x in devs if x["default"]), "") or t("st_null")
        QMessageBox.about(self, t("about"), t("about_text", VERSION, ff.version() or "—", ff.ffmpeg() or "—", dev,
                                              self.session.path))

    def maybe_check_updates(self) -> None:
        from . import updater
        if self.cfg.get("check_updates", True) and updater.can_self_update() \
                and time.time() - float(self.cfg.get("last_update_check", 0)) > self.UPDATE_EVERY:
            self.check_updates()

    def check_updates(self, manual: bool = False) -> None:
        from . import updater
        self._update_manual = manual
        self.cfg["last_update_check"] = int(time.time())
        config.save(self.cfg)

        def work():
            try:
                self.bridge.update.emit(updater.check(), "")
            except Exception as e:
                self.bridge.update.emit(None, str(e))
        threading.Thread(target=work, daemon=True).start()

    def _on_update_result(self, info, err: str) -> None:
        manual = getattr(self, "_update_manual", False)
        if err:
            if manual:
                QMessageBox.warning(self, APP_TITLE, t("upd_error", err))
            return
        if not info:
            if manual:
                QMessageBox.information(self, APP_TITLE, t("upd_none", VERSION))
            return
        self.update_info = info
        if manual or self.cfg.get("update_notified") != info["version"]:
            self.cfg["update_notified"] = info["version"]
            config.save(self.cfg)
            self.show_update()

    def show_update(self) -> None:
        info = self.update_info
        if not info:
            return
        from . import updater
        if not updater.can_self_update() or not info.get("url"):
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl(info["page"]))
            return
        if any(d.dirty for d in self.docs):
            QMessageBox.information(self, APP_TITLE, t("upd_unsaved"))
            return
        from .dialogs import UpdateDialog
        dlg = UpdateDialog(info, self)
        if dlg.exec() and dlg.path:
            self._installer = dlg.path
            self.close()

    # ── the window itself ─────────────────────────────────────
    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.open_paths(paths)

    def closeEvent(self, ev) -> None:
        if self.job:
            self.cancel_job()
            t0 = time.monotonic()
            while self.job and time.monotonic() - t0 < 5:
                QApplication.processEvents()
                time.sleep(0.02)
        for i in range(len(self.docs) - 1, -1, -1):
            d = self.docs[i]
            if d.dirty and not self._ask_save(d, i):
                ev.ignore()
                return
        self.player.stop()
        self.cfg["geometry"] = bytes(self.saveGeometry().toHex()).decode()
        config.save(self.cfg)
        for d in self.docs:
            d.close()
        self.docs.clear()
        if self._installer:
            from . import updater
            winint.release_mutex()
            try:
                updater.run_installer(self._installer)
            except OSError:
                pass
        ev.accept()
