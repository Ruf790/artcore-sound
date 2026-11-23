"""The main window: menus, the tool bar, a tab per open file, the overview strip, the waveform with the level meters
beside it, the transport (play cursor counter, selection fields, format) and the status bar. Long jobs (opening,
saving, effects) run in a thread with a progress bar and Cancel in the status bar.

0.4: the spectrum panel under the waveform, the view modes (wave / spectrogram / both), the Markers menu and the list
of markers and regions under the meters, the Record window behind the transport's red button."""
from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QIcon, QKeySequence
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar, QScrollBar, QSplitter,
                               QTabBar, QToolButton, QVBoxLayout, QWidget)

from . import APP_TITLE, AUDIO_EXT, VERSION, VIDEO_EXT, analysis, config, ff, keys, media, proc, style, winint
from . import spectrogram as sgm
from .doc import Document, fmt_time, parse_time
from .errors import human as human_error
from .i18n import fmt_bytes, t
from .filmwin import FilmCommands
from .player import Player, Volume
from .volui import STEP_DB, VolumeControl
from .waveview import Meters, Overview, WaveView

UNDO_KEYS = {"open", "new", "cut", "paste", "delete", "silence", "insert silence", "trim", "add marker", "add region",
             "move marker", "rename marker", "delete marker", "markers", "record"}
STATS_DELAY = 300            # ms after the last change of the selection before the status bar measures it
STATS_CACHE = 64
MODE_ICONS = {"wave": "fa6s.wave-square", "spec": "fa6s.fire", "both": "fa6s.layer-group"}


class Bridge(QObject):
    progress = Signal(float)
    done = Signal(object, object, object)       # result, error (str / None / "cancel"), the job's on_done
    update = Signal(object, str)
    stats = Signal(object, object)              # key, analysis.Stats (the status bar's background measuring)


class MainWindow(QMainWindow, FilmCommands):
    UPDATE_EVERY = 24 * 3600

    def __init__(self, cfg: dict, session):
        super().__init__()
        self.cfg, self.session = cfg, session
        self.docs: list[Document] = []
        self.clip = None
        self.volume = Volume(cfg.get("play_volume_db", 0.0), cfg.get("play_muted", False))   # what is heard (2.0)
        self.player = Player(cfg.get("device", ""), cfg.get("latency", "low"), self.volume)
        self._vol_save = QTimer(self)
        self._vol_save.setSingleShot(True)
        self._vol_save.setInterval(700)
        self._vol_save.timeout.connect(self._save_volume)
        self.video_win = None                   # 2.0: the video window (videoui.py)
        self.last_video_out = ""
        self.bridge = Bridge()
        self.bridge.progress.connect(self._on_progress)
        self.bridge.done.connect(self._on_job_done)
        self.bridge.update.connect(self._on_update_result)
        self.bridge.stats.connect(self._on_stats)
        self.stats_cache: dict = {}             # (folder, version, a, b) → analysis.Stats
        self._stats_key = None                  # what the status bar shows
        self._stats_run = None                  # (key, Progress) of the measuring in the background
        self._stats_timer = QTimer(self)
        self._stats_timer.setSingleShot(True)
        self._stats_timer.setInterval(STATS_DELAY)
        self._stats_timer.timeout.connect(self._stats_start)
        self.stats_dlg = None
        self.spectrum_dlg = None
        self.rec_dlg = None
        self.batch_win = None
        self._recorded = 0
        self.fx_dlg = None                      # the effect window while it is open
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
        self._jkeys: dict = {}                  # document → the journal key last written (recovery.py)
        self.journal_timer = QTimer(self)
        self.journal_timer.setInterval(1000)
        self.journal_timer.timeout.connect(self.write_journals)
        self.journal_timer.start()
        from . import crash
        crash.on_crash.append(lambda: self.write_journals(True))
        geo = cfg.get("geometry")
        if geo:
            from PySide6.QtCore import QByteArray
            self.restoreGeometry(QByteArray.fromHex(geo.encode()))
        self._update_all()

    # ── building ──────────────────────────────────────────────
    def _act(self, menu, icon: str, text: str, slot, keys=None) -> QAction:
        a = QAction(style.icon(icon) if icon else QIcon(), text, self)
        from . import crash
        a.triggered.connect(lambda _c=False, n=text: crash.note(n))
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
        from .anaui import SpectrumPanel
        from .markui import MarkerList
        self.wave = WaveView()
        self.wave.set_sg_settings(sgm.Settings.from_dict(self.cfg.get("spectrogram")))
        self.wave.set_mode(self.cfg.get("view_mode", "wave"))
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
        self.spec_panel = SpectrumPanel(self)
        self.spec_panel.closeRequested.connect(lambda: self.show_spectrum_panel(False))
        self.spec_panel.windowRequested.connect(self.spectrum_dialog)
        lv.addWidget(self.spec_panel)
        self.spec_panel.setVisible(bool(self.cfg.get("spectrum_panel_on", True)))
        mid.addWidget(left)
        right = QWidget()
        right.setObjectName("sidePanel")
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        side = QSplitter(Qt.Vertical)
        side.setChildrenCollapsible(False)
        box = QWidget()
        bv = QVBoxLayout(box)
        bv.setContentsMargins(6, 6, 6, 4)
        bv.setSpacing(4)
        cap = QLabel(t("meters"))
        cap.setObjectName("caption")
        bv.addWidget(cap)
        self.meters = Meters()
        bv.addWidget(self.meters, 1)
        side.addWidget(box)
        box = QWidget()
        bv = QVBoxLayout(box)
        bv.setContentsMargins(6, 4, 6, 6)
        bv.setSpacing(4)
        cap = QLabel(t("mk_list"))
        cap.setObjectName("caption")
        bv.addWidget(cap)
        self.mlist = MarkerList()
        bv.addWidget(self.mlist, 1)
        side.addWidget(box)
        side.setSizes(self.cfg.get("side_split") or [420, 300])
        self.side_split = side
        rv.addWidget(side)
        mid.addWidget(right)
        mid.setStretchFactor(0, 1)
        mid.setStretchFactor(1, 0)
        mid.setSizes([int(x) for x in self.cfg.get("main_split") or []] or [1080, 330])
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
        self.wave.markerMoved.connect(self._marker_moved)
        self.wave.markerRename.connect(self.rename_marker)
        self.mlist.activated.connect(self.goto_marker)
        self.mlist.renamed.connect(lambda mid, name: self._rename(mid, name))
        self.mlist.deleteRequested.connect(self.delete_markers)
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
        self.a_keys = A(m, "fa6s.keyboard", t("ks_title") + "…", self.keys_dialog, "F1")
        A(m, "fa6s.circle-info", t("about"), self.about)
        m.addSeparator()
        A(m, "", t("quit"), self.close, "Ctrl+Q")
        m = mb.addMenu(t("m_file"))
        self.a_open = A(m, "fa6s.folder-open", t("open") + "…", self.open_dialog, "Ctrl+O")
        self.recent_menu = m.addMenu(style.icon("fa6s.clock-rotate-left"), t("recent"))
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        m.addSeparator()
        self.a_save = A(m, "fa6s.floppy-disk", t("save"), self.save, "Ctrl+S")
        self.a_save_as = A(m, "fa6s.file-export", t("save_as") + "…", self.save_as, "Ctrl+Shift+S")
        self.a_save_sel = A(m, "fa6s.scissors", t("save_sel") + "…", lambda: self.save_as(selection=True))
        self.a_save_regions = A(m, "fa6s.layer-group", t("mk_save_regions") + "…", self.save_regions_dialog)
        self.a_return_video = A(m, "fa6s.film", t("rv_title") + "…", self.return_to_video_dialog, "Ctrl+Shift+E")
        m.addSeparator()
        self.a_show = A(m, "fa6s.folder", t("show_file"), self._show_file)
        m.addSeparator()
        self.a_close = A(m, "fa6s.xmark", t("close"), lambda: self.close_doc(self.tabs.currentIndex()), "Ctrl+W")
        self.a_close_all = A(m, "", t("close_all"), self.close_all, "Ctrl+Shift+W")
        m = mb.addMenu(t("m_edit"))
        self.a_undo = A(m, "fa6s.rotate-left", t("undo"), self.undo, "Ctrl+Z")
        self.a_redo = A(m, "fa6s.rotate-right", t("redo"), self.redo, ["Ctrl+Y", "Ctrl+Shift+Z"])
        self.a_undo.setProperty("acName", t("undo"))
        self.a_redo.setProperty("acName", t("redo"))
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
        m = mb.addMenu(t("m_markers"))
        self.mk_actions = {}
        for key, icon, text, slot, seq in (
                ("add", "fa6s.flag", t("mk_add"), self.add_marker, "M"),
                ("region", "fa6s.arrows-left-right-to-line", t("mk_add_region"), self.add_region, "R"),
                (None, None, None, None, None),
                ("prev", "fa6s.backward-step", t("mk_prev"), lambda: self.jump_marker(-1), "Ctrl+Left"),
                ("next", "fa6s.forward-step", t("mk_next"), lambda: self.jump_marker(1), "Ctrl+Right"),
                (None, None, None, None, None),
                ("rename", "fa6s.pen", t("mk_rename") + "…", lambda: self.rename_marker(None), None),
                ("delete", "fa6s.trash-can", t("mk_delete"), lambda: self.delete_markers(None), None),
                ("delete_all", "", t("mk_delete_all"), self.delete_all_markers, None),
                (None, None, None, None, None),
                ("import", "fa6s.file-import", t("mk_import") + "…", self.import_markers, None),
                ("export", "fa6s.file-export", t("mk_export") + "…", self.export_markers, None)):
            if key is None:
                m.addSeparator()
                continue
            self.mk_actions[key] = A(m, icon, text, slot, seq)
        m.addSeparator()
        m.addAction(self.a_save_regions)
        m = mb.addMenu(t("m_view"))
        self.a_zoom_in = A(m, "fa6s.magnifying-glass-plus", t("zoom_in"), lambda: self.wave.zoom(0.5), ["Up", "Ctrl+="])
        self.a_zoom_out = A(m, "fa6s.magnifying-glass-minus", t("zoom_out"), lambda: self.wave.zoom(2.0),
                            ["Down", "Ctrl+-"])
        self.a_zoom_all = A(m, "fa6s.expand", t("zoom_all"), self.wave.zoom_all, "Ctrl+Up")
        self.a_zoom_sel = A(m, "fa6s.arrows-left-right", t("zoom_sel"), self._zoom_sel, "Ctrl+Down")
        self.a_zoom_smp = A(m, "fa6s.wave-square", t("zoom_samples"), self.wave.zoom_samples, "Ctrl+Shift+Up")
        m.addSeparator()
        vm = m.addMenu(style.icon("fa6s.chart-area"), t("vm_mode"))
        grp = QActionGroup(self)
        self.mode_actions = {}
        for k, seq in (("wave", "Alt+1"), ("spec", "Alt+2"), ("both", "Alt+3")):
            a = A(vm, MODE_ICONS[k], t("vm_" + k), lambda k=k: self.set_view_mode(k), seq)
            a.setCheckable(True)
            a.setChecked(self.cfg.get("view_mode", "wave") == k)
            grp.addAction(a)
            self.mode_actions[k] = a
        self.a_sg_settings = A(m, "fa6s.palette", t("sgd_title") + "…", self.spectrogram_dialog)
        self.a_spec_panel = A(m, "fa6s.chart-line", t("sp_panel"), lambda: self.show_spectrum_panel(None), "F6")
        self.a_spec_panel.setCheckable(True)
        self.a_spec_panel.setChecked(bool(self.cfg.get("spectrum_panel_on", True)))
        m.addSeparator()
        self.a_follow = A(m, "", t("follow"), self._toggle_follow)
        self.a_follow.setCheckable(True)
        self.a_follow.setChecked(self.cfg.get("follow", True))
        m.addSeparator()
        self.a_go_start = A(m, "fa6s.backward-step", t("go_start"), self.go_start, "Home")
        self.a_go_end = A(m, "fa6s.forward-step", t("go_end"), self.go_end, "End")
        self.a_rewind = A(m, "", t("rewind"), lambda: self.nudge(-1), "Left")
        self.a_forward = A(m, "", t("forward"), lambda: self.nudge(1), "Right")
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
                                ("fa6s.arrows-up-to-line", "p_dc", self.remove_dc),
                                (None, None, None),
                                ("fa6s.down-left-and-up-right-to-center", "p_trim_silence", self.trim_silence_dialog),
                                (None, None, None),
                                ("fa6s.gauge", "p_resample", self.resample_dialog),
                                ("fa6s.code-branch", "p_channels", self.channels_dialog),
                                ("fa6s.compress", "p_to_mono", self.to_mono_dialog),
                                ("fa6s.expand", "p_to_stereo", self.to_stereo)):
            if key is None:
                m.addSeparator()
                continue
            dots = "…" if key in ("p_gain", "p_normalize", "p_resample", "p_channels", "p_trim_silence",
                                  "p_to_mono") else ""
            act = A(m, icon, t(key) + dots, slot)
            act.setObjectName(key)
            self.proc_actions.append(act)
        self.a_to_mono = next(a for a in self.proc_actions if a.objectName() == "p_to_mono")
        self.a_to_stereo = next(a for a in self.proc_actions if a.objectName() == "p_to_stereo")
        m = mb.addMenu(t("m_effects"))
        self.fx_actions = {}
        for key, icon in (("eq", "fa6s.sliders"), ("geq", "fa6s.chart-column"), (None, None),
                          ("comp", "fa6s.compress"), ("limit", "fa6s.arrows-down-to-line"), (None, None),
                          ("delay", "fa6s.clone"), ("reverb", "fa6s.church"), (None, None),
                          ("tempo", "fa6s.gauge-high"), (None, None),
                          ("take_noise", "fa6s.eye-dropper"), ("nr", "fa6s.wand-magic-sparkles"),
                          ("declick", "fa6s.bolt")):
            if key is None:
                m.addSeparator()
                continue
            if key == "take_noise":
                act = A(m, icon, t("fx_take_noise"), self.take_noise_sample)
                self.fx_actions["save_noise"] = A(m, "fa6s.floppy-disk", t("nr_save_sample") + "…",
                                                  self.save_noise_sample)
                self.fx_actions["load_noise"] = A(m, "fa6s.folder-open", t("nr_load_sample") + "…",
                                                  self.load_noise_sample)
            else:
                act = A(m, icon, t("fx_" + key) + "…", lambda k=key: self.effect_dialog(k))
            act.setObjectName("fx_" + key)
            self.fx_actions[key] = act
        self.build_film_menu(mb, A)                 # 2.0: Фильм / Film
        m = mb.addMenu(t("m_tools"))
        self.a_stats = A(m, "fa6s.table-list", t("stats") + "…", self.statistics_dialog, "Ctrl+I")
        self.a_spectrum = A(m, "fa6s.chart-line", t("sp_title") + "…", self.spectrum_dialog, "Ctrl+Shift+F")
        m.addSeparator()
        self.a_sg_mode = A(m, "fa6s.chart-area", t("sg_toggle"), self._toggle_spectrogram, "F7")
        m.addAction(self.a_sg_settings)
        m.addSeparator()
        self.a_batch = A(m, "fa6s.layer-group", t("bt_title") + "…", self.batch_window, "Ctrl+B")
        m = mb.addMenu(t("m_transport"))
        self.a_play = A(m, "fa6s.play", t("play"), self.play_toggle, "Space")
        self.a_play_all = A(m, "fa6s.circle-play", t("play_all"), self.play_all, "Shift+Space")
        self.a_pause = A(m, "fa6s.pause", t("pause"), self.pause, "Ctrl+Space")
        self.a_stop = A(m, "fa6s.stop", t("stop"), self.stop_play)
        self.a_loop = A(m, "fa6s.repeat", t("loop"), self._toggle_loop, "Q")
        self.a_loop.setCheckable(True)
        self.a_loop.setChecked(self.cfg.get("loop", False))
        m.addSeparator()
        self.a_record = A(m, "fa6s.circle", t("record") + "…", self.record_toggle, "Ctrl+R")
        m.addSeparator()
        self.a_vol_up = A(m, "fa6s.volume-high", t("vol_up"), lambda: self.vol.step(STEP_DB), "Ctrl+Alt+Up")
        self.a_vol_down = A(m, "fa6s.volume-low", t("vol_down"), lambda: self.vol.step(-STEP_DB), "Ctrl+Alt+Down")
        self.a_mute = A(m, "fa6s.volume-xmark", t("vol_mute_act"), lambda: self.vol.toggle_mute(), "Ctrl+Alt+M")
        self.a_mute.setCheckable(True)
        self.a_mute.setChecked(self.volume.muted)
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
        K = keys.tip                                                      # noqa: N806 — the tips from the actions
        for key, icon, tip, slot in (("open", "fa6s.folder-open", K(self.a_open, t("open_tip")), self.open_dialog),
                                     ("save", "fa6s.floppy-disk", K(self.a_save, t("save_tip")), self.save),
                                     ("|", None, None, None),
                                     ("undo", "fa6s.rotate-left", K(self.a_undo), self.undo),
                                     ("redo", "fa6s.rotate-right", K(self.a_redo), self.redo),
                                     ("|", None, None, None),
                                     ("cut", "fa6s.scissors", K(self.a_cut), self.cut),
                                     ("copy", "fa6s.copy", K(self.a_copy), self.copy),
                                     ("paste", "fa6s.paste", K(self.a_paste), self.paste),
                                     ("delete", "fa6s.trash-can", K(self.a_delete), self.delete),
                                     ("|", None, None, None),
                                     ("trim", "fa6s.crop-simple", K(self.a_trim), self.trim),
                                     ("silence", "fa6s.volume-xmark", K(self.a_silence), self.silence),
                                     ("|", None, None, None),
                                     ("zoom_in", "fa6s.magnifying-glass-plus", K(self.a_zoom_in),
                                      lambda: self.wave.zoom(0.5)),
                                     ("zoom_out", "fa6s.magnifying-glass-minus", K(self.a_zoom_out),
                                      lambda: self.wave.zoom(2.0)),
                                     ("zoom_all", "fa6s.expand", K(self.a_zoom_all), self.wave.zoom_all),
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
        h.addWidget(self._sep())
        for key, icon, text, tip, fxk in (("eq", "fa6s.sliders", t("tb_eq"), t("tb_eq_tip"), "eq"),
                                          ("nr", "fa6s.wand-magic-sparkles", t("tb_nr"), t("tb_nr_tip"), "nr")):
            self.tb["fx_" + key] = T(icon, tip, lambda k=fxk: self.effect_dialog(k), text)
            h.addWidget(self.tb["fx_" + key])
        h.addWidget(self._sep())
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for k in ("wave", "spec", "both"):
            b = T(MODE_ICONS[k], K(self.mode_actions[k]), lambda k=k: self.set_view_mode(k), checkable=True)
            b.setChecked(self.cfg.get("view_mode", "wave") == k)
            self.mode_group.addButton(b)
            self.tb["mode_" + k] = b
            h.addWidget(b)
        self.tb["spectrum"] = T("fa6s.chart-line", K(self.a_spec_panel), lambda: self.show_spectrum_panel(None),
                                checkable=True)
        self.tb["spectrum"].setChecked(bool(self.cfg.get("spectrum_panel_on", True)))
        h.addWidget(self.tb["spectrum"])
        h.addWidget(self._sep())
        self.tb["marker"] = T("fa6s.flag", K(self.mk_actions["add"]), self.add_marker)
        h.addWidget(self.tb["marker"])
        self.tb["region"] = T("fa6s.arrows-left-right-to-line", K(self.mk_actions["region"]), self.add_region)
        h.addWidget(self.tb["region"])
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
        K = keys.tip                                                      # noqa: N806
        self.t_start = T("fa6s.backward-step", K(self.a_go_start), self.go_start)
        self.t_rew = T("fa6s.backward", K(self.a_rewind), lambda: self.nudge(-1))
        self.t_play = T("fa6s.play", K(self.a_play, t("play_tip")), self.play_toggle)
        self.t_play.setObjectName("play")
        self.t_play.setIconSize(self.t_play.iconSize() * 1.25)
        self.t_pause = T("fa6s.pause", K(self.a_pause), self.pause)
        self.t_stop = T("fa6s.stop", K(self.a_stop), self.stop_play)
        self.t_ff = T("fa6s.forward", K(self.a_forward), lambda: self.nudge(1))
        self.t_end = T("fa6s.forward-step", K(self.a_go_end), self.go_end)
        self.t_loop = T("fa6s.repeat", K(self.a_loop), self._toggle_loop, checkable=True)
        self.t_loop.setChecked(self.cfg.get("loop", False))
        self.t_rec = T("fa6s.circle", K(self.a_record), self.record_toggle)
        self.t_rec.setIcon(style.icon("fa6s.circle", "#e5484d"))
        for b in (self.t_start, self.t_rew, self.t_play, self.t_pause, self.t_stop, self.t_ff, self.t_end,
                  self.t_loop, self.t_rec):
            h.addWidget(b)
        h.addSpacing(10)
        self.vol = VolumeControl(self.volume)
        self.vol.changed.connect(self._volume_changed)
        h.addWidget(self.vol)
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
        self.st_stats = QLabel("")
        self.st_stats.setToolTip(t("st_stats_tip"))
        self.st_stats.setTextFormat(Qt.RichText)
        self.st_sel = QLabel("")
        self.st_disk = QLabel("")
        self.st_dev = QLabel("")
        for w in (self.st_stats, self.st_sel, self.st_disk, self.st_dev):
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
        self.mlist.fill(d)
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
            QMessageBox.warning(self, APP_TITLE, t("open_failed", os.path.basename(path), human_error(e)))
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
                self.bridge.done.emit(None, human_error(e), on_done)
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

    def collect_garbage(self) -> int:
        """Delete the working files of the sources only a discarded redo branch used — once neither the clipboard
        nor another document (a paste) holds them. A long film's effect undone and replaced frees its gigabytes at
        once instead of at the exit. → how many sources were deleted."""
        if not any(d.orphans for d in self.docs):
            return 0
        live = set()
        for d in self.docs:
            live |= {id(s) for s in d.sources()}
        if self.clip is not None:
            live |= {id(s) for s, _o, _n in self.clip.pieces}
        return sum(d.free_orphans(live) for d in self.docs)

    def _edited(self) -> None:
        self.collect_garbage()
        d = self.doc
        if d is not None:
            self.meters.set_channels(d.labels)            # undo of a channel change changes the meters too
        self.wave.refresh()
        self.overview.update()
        self.mlist.fill(d)
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
        f = dlg.time_field(t("length"), int(float(self.cfg.get("silence_s", 1.0)) * d.rate), d.rate)
        if dlg.exec():
            n = ValueDialog.frames_of(f)
            if n:
                self.cfg["silence_s"] = n / d.rate
                config.save(self.cfg)
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
                  self.a_ins_sil, None, self.mk_actions["add"], self.mk_actions["region"], None, self.a_sel_all,
                  self.a_zoom_sel, self.a_zoom_all, None, *self.proc_actions[:4]):
            if a is None:
                m.addSeparator()
            else:
                m.addAction(a)
        m.exec(pos)

    # ── processing ────────────────────────────────────────────
    def _process(self, label: str, make, rng=None, meta=None, after=None) -> None:
        """make(progress) → a new source for rng (default: the selection or the whole file) in a thread; after()
        once the result is in the document."""
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
            if after is not None:
                after()
        if whole and not had_sel and not meta:
            self._toast(t("p_whole"))
        self._make_room(d, b - a)
        self.run_job(t("processing", label), make, done, d)

    def _make_room(self, d: Document, frames: int) -> None:
        """Before a job writes `frames` new frames: when the disk of the working files has not that much room,
        the steps Redo would bring back are forgotten and their files deleted first (a toast says so)."""
        need = int(frames * d.channels * 4 * 1.05)
        free = winint.free_space(self.session.path)
        if free < 0 or free >= need or not d.can_redo():
            return
        d.discard_redo()
        n = self.collect_garbage()
        self._update_actions()
        self._toast(t("room_made", fmt_bytes(winint.free_space(self.session.path)), n))

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
        s = dlg.spin(t("p_gain_by"), float(self.cfg.get("gain_db", 3.0)), -96, 48, " dB")
        if dlg.exec():
            self.cfg["gain_db"] = s.value()
            config.save(self.cfg)
            self.simple_op("gain", db=s.value())

    def normalize_dialog(self) -> None:
        """By the peak or by the loudness (EBU R128) with a true-peak ceiling; says when the ceiling held the
        gain back."""
        d = self.doc
        if d is None or self._busy() or d.frames <= 0:
            return
        from .dialogs import NormalizeDialog, fmt_db
        a, b = self._range(d)
        pk = float(d.peak(a, b).max())
        if pk <= 0:
            QMessageBox.information(self, APP_TITLE, t("p_silent"))
            return
        now = t("p_norm_now", fmt_db(proc.gain_to_db(pk), 2))
        st = self.stats_cache.get(self.stats_key(d, a, b))
        if st is not None and st.loudness:
            now += " · " + t("nz_now_lufs", fmt_db(st.integrated))
        dlg = NormalizeDialog(self.cfg, now, not d.sel, self)
        if not dlg.exec():
            return
        opts = dlg.settings()
        self.cfg["normalize"] = opts
        config.save(self.cfg)
        mode = opts["mode"]
        target = opts["target"] if mode == "loudness" else opts["peak"]
        out = {}

        def make(pr):
            src, res = proc.normalize(d, a, b, mode, target, opts["ceiling"], opts["peak_tp"], pr)
            out["res"] = res
            return src
        label = t("p_norm_lufs", fmt_db(target)) if mode == "loudness" else t("p_normalize")
        self._process(label, make, (a, b), after=lambda: self._normalized(out.get("res")))

    def _normalized(self, res) -> None:
        from .dialogs import fmt_db
        if res is None:
            return
        if res.limited:
            QMessageBox.information(self, APP_TITLE, t("nz_limited", fmt_db(res.gain_db, 2), fmt_db(res.after),
                                                       fmt_db(res.true_peak_after)))
        elif res.mode == "loudness":
            self._toast(t("nz_done", fmt_db(res.before), fmt_db(res.after), fmt_db(res.gain_db, 2)))

    def remove_dc(self) -> None:
        d = self.doc
        if d is None or d.frames <= 0:
            return
        a, b = self._range(d)
        out = {}

        def make(pr):
            src, dc = proc.remove_dc(d, a, b, pr)
            out["dc"] = dc
            return src

        def after():
            from .dialogs import fmt_db
            if "dc" in out:
                self._toast(t("dc_done", ", ".join(f"{lab} {fmt_db(v * 100, 3)} %"
                                                   for lab, v in zip(d.labels, out["dc"]))))
        self._process(t("p_dc"), make, (a, b), after=after)

    def trim_silence_dialog(self) -> None:
        d = self.doc
        if d is None or self._busy() or d.frames <= 0:
            return
        from .dialogs import ValueDialog
        o = self.cfg.get("trim_silence") or {}
        dlg = ValueDialog(t("ts_title"), self, t("ts_note") + ("" if d.sel else "\n" + t("ts_whole")))
        thr = dlg.spin(t("ts_threshold"), float(o.get("threshold", -60.0)), -120, 0, " dBFS", 1, 1.0)
        mar = dlg.spin(t("ts_margin"), float(o.get("margin", 50)), 0, 10000, " ms", 0, 10)
        if not dlg.exec():
            return
        self.cfg["trim_silence"] = {"threshold": thr.value(), "margin": mar.value()}
        config.save(self.cfg)
        d = self._edit_doc()
        if d is None:
            return
        a, b = self._range(d)
        edges = proc.sound_edges(d, a, b, thr.value(), int(round(mar.value() * d.rate / 1000)))
        if edges is None:
            QMessageBox.information(self, APP_TITLE, t("ts_all_quiet", f"{thr.value():.0f}"))
            return
        if edges == (a, b):
            self._toast(t("ts_nothing"))
            return
        cut = (edges[0] - a, b - edges[1])
        d.keep(a, b, *edges, label=t("p_trim_silence"))
        self._edited()
        self._toast(t("ts_done", fmt_time(cut[0], d.rate), fmt_time(cut[1], d.rate)))

    def to_mono_dialog(self) -> None:
        d = self.doc
        if d is None or self._busy() or d.frames <= 0 or d.channels != 2:
            return
        from .dialogs import ValueDialog
        dlg = ValueDialog(t("tm_title"), self, t("tm_note"))
        c = dlg.combo(t("tm_how"), [(t("tm_mix"), "mix"), (t("tm_left"), "left"), (t("tm_right"), "right")],
                      self.cfg.get("to_mono", "mix"))
        if not dlg.exec():
            return
        mode = c.currentData()
        self.cfg["to_mono"] = mode
        config.save(self.cfg)
        self._process(t("p_to_mono") + f" ({t('tm_' + mode).lower()})", lambda pr: proc.stereo_to_mono(d, mode, pr),
                      (0, d.frames), {"channels": 1, "layout": "mono", "labels": ("M",)})

    def to_stereo(self) -> None:
        d = self.doc
        if d is None or self._busy() or d.frames <= 0 or d.channels != 1:
            return
        self._process(t("p_to_stereo"), lambda pr: proc.mono_to_stereo(d, pr), (0, d.frames),
                      {"channels": 2, "layout": "stereo", "labels": ("L", "R")})

    def resample_dialog(self) -> None:
        d = self.doc
        if d is None or self._busy() or d.frames <= 0:
            return
        from .dialogs import RATES, ValueDialog
        dlg = ValueDialog(t("rs_title"), self, t("rs_note"))
        last = int(self.cfg.get("resample_rate") or 0)
        c = dlg.combo(t("rs_rate"), [(f"{r} Hz", r) for r in RATES],
                      last if last and last != d.rate else (48000 if d.rate != 48000 else 44100))
        if dlg.exec() and c.currentData() != d.rate:
            rate = int(c.currentData())
            self.cfg["resample_rate"] = rate
            config.save(self.cfg)
            self._process(t("p_resample") + f" {rate} Hz", lambda pr: proc.convert_document(d, rate, d.channels,
                                                                                            d.layout, pr),
                          (0, d.frames), {"rate": rate})

    def channels_dialog(self) -> None:
        d = self.doc
        if d is None or self._busy() or d.frames <= 0:
            return
        from .dialogs import ValueDialog
        from . import chans
        items = [(t("ch_mono"), 1), (t("ch_stereo"), 2), ("5.1", 6)]
        dlg = ValueDialog(t("chn_title"), self, t("chn_note"))
        last = int(self.cfg.get("channels_to") or 0)
        c = dlg.combo(t("chn_to"), items, last if last and last != d.channels else (2 if d.channels != 2 else 1))
        meth = dlg.combo(t("mx_method"), [], None)                  # 2.0: how 5.1 → 2 and 2 → 5.1 go

        def methods():
            want = int(c.currentData())
            meth.clear()
            if d.channels >= 5 and want == 2:
                for k in chans.DOWNMIX:
                    meth.addItem(t("mx_" + k), k)
            elif d.channels <= 2 and want == 6:
                meth.addItem(t("mx_upmix"), "upmix")
                meth.addItem(t("mx_plain"), "auto")
            else:
                meth.addItem(t("mx_plain"), "auto")
            i = meth.findData(self.cfg.get("channels_method"))
            meth.setCurrentIndex(max(0, i))
        c.currentIndexChanged.connect(lambda _i: methods())
        methods()
        if dlg.exec() and c.currentData() != d.channels:
            ch = int(c.currentData())
            how = meth.currentData() or "auto"
            self.cfg["channels_to"] = ch
            self.cfg["channels_method"] = how
            config.save(self.cfg)
            lay = ff.layout_for(ch)
            name = t("p_channels") + f": {lay}" + ("" if how == "auto" else f" ({t('mx_' + how)})")
            self._process(name, lambda pr: chans.convert(d, ch, how, pr),
                          (0, d.frames), {"channels": ch, "layout": lay, "labels": tuple(ff.labels(ch, lay))})

    # ── effects (0.3) ─────────────────────────────────────────
    def effect_dialog(self, key: str) -> None:
        """The window of an effect over the selection (or the whole file); Apply runs it as a job."""
        d = self.doc
        if d is None or self._busy() or d.frames <= 0:
            return
        from . import fxui
        self.stop_play()
        a, b = self._range(d)
        whole = not d.sel
        dlg = fxui.EffectDialog(self, key, d, a, b, whole)
        self.fx_dlg = dlg
        ok = dlg.exec()
        self.fx_dlg = None
        if not ok:
            return
        self.apply_effect(key, dlg.result_values(), dlg.keep_tail_on(), (a, b))

    def apply_effect(self, key: str, values: dict, keep_tail: bool = False, rng=None) -> None:
        from . import fx
        d = self._edit_doc()
        if d is None or d.frames <= 0:
            return
        eff = fx.get(key)
        a, b = rng or self._range(d)
        had_sel = d.sel
        label = eff.label(values)
        info = {}

        def work(pr):
            return fx.render(d, a, b, eff, values, pr, keep_tail, info)

        def done(src, err):
            if err:
                if err != "cancel":
                    QMessageBox.warning(self, APP_TITLE, t("op_failed", err))
                return
            end = a + src.frames if eff.realtime else b         # a kept tail reaches past b
            d.replace(a, end, src, label, sel=(a, a + src.frames) if had_sel else None)
            self._edited()
            proc = info.get("proc")
            if key == "declick" and proc is not None:
                self._toast(t("declick_done", proc.found))
        if not had_sel:
            self._toast(t("p_whole"))
        self._make_room(d, b - a)
        self.run_job(t("processing", label), work, done, d)

    def take_noise_sample(self) -> None:
        """Effects → Take a noise sample: the noise spectrum of the selection, kept for the session."""
        from . import fx
        d = self.doc
        if d is None:
            return
        if not d.sel or d.sel[1] - d.sel[0] < 0.1 * d.rate:
            QMessageBox.information(self, APP_TITLE, t("nr_need_sel"))
            return
        a, b = d.sel
        b = min(b, a + 30 * d.rate)                 # half a minute of noise is plenty
        try:
            prof = fx.take_noise_profile(d, a, b)
        except ValueError:
            QMessageBox.information(self, APP_TITLE, t("nr_need_sel"))
            return
        self._toast(t("nr_taken", f"{prof['seconds']:.2f}"))

    def save_noise_sample(self) -> None:
        """Effects → Save the noise sample…: the sample taken into a file (*.acnoise) — the batch's noise reduction
        and a later session use it."""
        from . import batch, fx
        prof = fx.get("nr").profile
        if prof is None:
            QMessageBox.information(self, APP_TITLE, t("nr_no_sample"))
            return
        d = self.doc
        stem = os.path.splitext(d.name)[0] if d is not None and d.name else "noise"
        start = os.path.join(self.cfg.get("noise_dir") or self.cfg.get("save_dir") or "", f"{stem} noise{batch.NOISE_EXT}")
        path, _f = QFileDialog.getSaveFileName(self, t("nr_save_sample"), start,
                                               t("nr_file_filter") + f" (*{batch.NOISE_EXT})")
        if not path:
            return
        if not path.lower().endswith(batch.NOISE_EXT):
            path += batch.NOISE_EXT
        try:
            batch.save_noise_profile(path, prof)
        except OSError as e:
            QMessageBox.warning(self, APP_TITLE, human_error(e))
            return
        self.cfg["noise_dir"] = os.path.dirname(path)
        config.save(self.cfg)
        self._toast(t("nr_saved", os.path.basename(path)))

    def load_noise_sample(self) -> None:
        from . import batch, fx
        path, _f = QFileDialog.getOpenFileName(self, t("nr_load_sample"), self.cfg.get("noise_dir") or "",
                                               t("nr_file_filter") + f" (*{batch.NOISE_EXT})")
        if not path:
            return
        try:
            prof = batch.load_noise_profile(path)
        except (OSError, ValueError, KeyError) as e:
            QMessageBox.warning(self, APP_TITLE, t("bt_profile_bad", os.path.basename(path)) + "\n" + str(e))
            return
        d = self.doc
        if d is not None and d.rate != prof["rate"]:
            prof = batch.adapt_noise_profile(prof, d.rate)
        fx.get("nr").profile = prof
        self.cfg["noise_dir"] = os.path.dirname(path)
        config.save(self.cfg)
        self._toast(t("nr_loaded", os.path.basename(path), f"{prof.get('seconds', 0):.2f}"))

    def keys_dialog(self) -> None:
        from .dialogs import KeysDialog
        KeysDialog(self.menuBar(), self).exec()

    # ── recovery after a crash (1.0) ──────────────────────────
    def write_journals(self, force: bool = False) -> None:
        """The journal of every open document whose piece table, history or markers changed (recovery.py)."""
        from . import recovery
        for d in list(self.docs):
            if not d.history:
                continue
            key = recovery.journal_key(d)
            if not force and self._jkeys.get(id(d)) == key:
                continue
            try:
                recovery.write(d, self.session.path)
                self._jkeys[id(d)] = key
            except (OSError, ValueError):
                pass                                  # a full disk: the next change tries again

    def offer_recovery(self, entries) -> int:
        """At the start: the unsaved documents of a run that crashed — restore the chosen ones, remove the rest.
        → how many were restored."""
        from . import recovery, store
        from .dialogs import RecoveryDialog
        dlg = RecoveryDialog(entries, self)
        chosen = dlg.chosen() if dlg.exec() else []
        n = 0
        failed = []
        for e in chosen:
            try:
                d = recovery.restore(e, self.session)
            except (OSError, ValueError, KeyError, IndexError) as ex:
                failed.append(f"{e.name}: {human_error(ex)}")
                continue
            self.add_doc(d)
            n += 1
        store.clean_stale(self.session.root)
        if failed:
            QMessageBox.warning(self, APP_TITLE, t("rc_failed", "\n".join(failed)))
        if n:
            self._toast(t("rc_done", n))
        return n

    # ── batch processing (1.0) ────────────────────────────────
    def batch_window(self) -> None:
        from .batchui import BatchWindow
        if self.batch_win is None:
            self.batch_win = BatchWindow(self)
            self.batch_win.finished.connect(self._batch_closed)
        self.batch_win.show()
        self.batch_win.raise_()
        self.batch_win.activateWindow()

    def _batch_closed(self, _r=0) -> None:
        if self.batch_win is not None:
            self.batch_win.deleteLater()
        self.batch_win = None

    # ── markers and regions (0.4) ─────────────────────────────
    def _markers_changed(self) -> None:
        d = self.doc
        self.mlist.fill(d)
        self.wave.update()
        self.overview.update()
        self._update_actions()

    def add_marker(self) -> None:
        """M: a marker at the cursor — while playing where it plays (the playback goes on)."""
        d = self.doc
        if d is None or self.job:
            return
        pos = self.player.position() if self.player.state == "play" and self.player.doc is d else d.cursor
        m = d.add_marker(pos, t("mk_marker_n", self._next_number(d, False)))
        self._markers_changed()
        self.mlist.select_id(m.id)
        self._toast(t("mk_added", m.name, fmt_time(m.pos, d.rate)))

    def add_region(self) -> None:
        """R: a region of the selection."""
        d = self.doc
        if d is None or self.job:
            return
        if not d.sel:
            self._toast(t("mk_need_sel"))
            return
        m = d.add_region(*d.sel, t("mk_region_n", self._next_number(d, True)))
        if m is not None:
            self._markers_changed()
            self.mlist.select_id(m.id)

    @staticmethod
    def _next_number(d, region: bool) -> int:
        return sum(1 for m in d.markers if (m.end is not None) == region) + 1

    def jump_marker(self, direction: int) -> None:
        """Ctrl+← / Ctrl+→: the cursor to the previous / next marker or region edge."""
        d = self.doc
        if d is None:
            return
        playing = self.player.state == "play" and self.player.doc is d
        here = self.player.position() if playing else d.cursor
        pts = d.marker_points()
        if direction < 0:
            cand = [p for p in pts if p < here - (d.rate // 4 if playing else 0)]
            target = cand[-1] if cand else None
        else:
            cand = [p for p in pts if p > here]
            target = cand[0] if cand else None
        if target is None:
            return
        if playing:
            self._on_seek(target)
            return
        d.cursor = target
        d.sel = None
        self.wave.ensure_visible(target)
        self._on_selection()
        self.wave.update()

    def goto_marker(self, mid: int) -> None:
        """A double click in the list: a marker → the cursor there, a region → selected."""
        d = self.doc
        m = d.marker(mid) if d else None
        if m is None:
            return
        if m.end is not None:
            d.sel = (m.pos, m.end)
            d.cursor = m.pos
            a, b = self.wave.visible()
            if not (a <= m.pos and m.end <= b):
                if m.end - m.pos > (b - a):
                    self.wave.zoom_range(m.pos, m.end)
                else:
                    self.wave.ensure_visible(m.pos)
        else:
            d.sel = None
            d.cursor = m.pos
            self.wave.ensure_visible(m.pos)
            if self.player.state == "play" and self.player.doc is d:
                self._on_seek(m.pos)
        self._on_selection()
        self.wave.update()

    def _marker_moved(self, mid: int, pos: int, end) -> None:
        d = self.doc
        if d is None:
            return
        d.update_marker(mid, pos=pos, end=end, label="move marker")
        self._markers_changed()

    def rename_marker(self, mid=None) -> None:
        d = self.doc
        if d is None:
            return
        if mid is None:
            ids = self.mlist.selected_ids()
            mid = ids[0] if ids else None
        m = d.marker(mid) if mid is not None else None
        if m is None:
            return
        name, ok = QInputDialog.getText(self, t("mk_rename"), t("mk_name"), QLineEdit.Normal, m.name)
        if ok:
            self._rename(mid, name.strip())

    def _rename(self, mid: int, name: str) -> None:
        d = self.doc
        if d is None or d.marker(mid) is None:
            return
        d.update_marker(mid, name=name, label="rename marker")
        self._markers_changed()

    def delete_markers(self, ids=None) -> None:
        d = self.doc
        if d is None:
            return
        if ids is None:
            ids = self.mlist.selected_ids()
            if not ids and d.sel:                      # the markers in the selection
                a, b = d.sel
                ids = [m.id for m in d.markers if a <= m.pos <= b]
        if ids:
            d.remove_markers(ids, label="delete marker")
            self._markers_changed()

    def delete_all_markers(self) -> None:
        d = self.doc
        if d and d.markers:
            d.remove_markers([m.id for m in d.markers], label="delete marker")
            self._markers_changed()

    def import_markers(self, path: str | None = None) -> None:
        d = self.doc
        if d is None:
            return
        from . import marks
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, t("mk_import"), self.cfg.get("markers_dir") or
                                                  (os.path.dirname(d.path) if d.path else ""),
                                                  f"{t('mk_lists')} (*.csv *.cue *.txt);;{t('files_all')} (*)")
            if not path:
                return
        self.cfg["markers_dir"] = os.path.dirname(path)
        try:
            items = marks.import_file(path, d.rate, d.frames)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, APP_TITLE, t("op_failed", e))
            return
        if not items:
            self._toast(t("mk_none_found"))
            return
        d.set_markers(items, label="markers")
        self._markers_changed()
        self._toast(t("mk_imported", len(items)))

    def export_markers(self, path: str | None = None) -> None:
        d = self.doc
        if d is None or not d.markers:
            self._toast(t("mk_none"))
            return
        from . import marks
        if path is None:
            stem = os.path.splitext(d.name)[0] if d.name else "markers"
            folder = self.cfg.get("markers_dir") or (os.path.dirname(d.path) if d.path else os.path.expanduser("~"))
            path, flt = QFileDialog.getSaveFileName(self, t("mk_export"), os.path.join(folder, stem + ".csv"),
                                                    "CSV (*.csv);;CUE (*.cue)")
            if not path:
                return
            if not os.path.splitext(path)[1]:
                path += ".cue" if "cue" in flt.lower() else ".csv"
        self.cfg["markers_dir"] = os.path.dirname(path)
        try:
            marks.export_file(path, d.markers, d.rate, d.path or d.name)
        except OSError as e:
            QMessageBox.warning(self, APP_TITLE, t("save_failed", os.path.basename(path), e))
            return
        self._toast(t("saved", path))

    def save_regions_dialog(self) -> None:
        d = self.doc
        if d is None or self._busy():
            return
        if not any(m.end is not None for m in d.markers):
            QMessageBox.information(self, APP_TITLE, t("rg_none"))
            return
        from .markui import RegionsDialog
        fmt = d.save_format or self.cfg.get("save_format") or "wav"
        fmt = fmt if fmt in media.FORMATS else "wav"
        folder = os.path.dirname(d.path) if d.path else (self.cfg.get("save_dir") or os.path.expanduser("~"))
        dlg = RegionsDialog(d, folder, fmt, self.cfg.get("save_opts", {}).get(fmt, ""), self)
        if not dlg.exec():
            return
        self.save_regions(dlg.path(), dlg.format(), dlg.option(), dlg.numbered.isChecked())

    def save_regions(self, folder: str, fmt: str, opt: str, numbered: bool = True, wait: bool = False) -> list:
        """Every region into a file of its own in folder (its name from the region). → the paths."""
        from .markui import region_paths
        d = self.doc
        if d is None or not folder:
            return []
        os.makedirs(folder, exist_ok=True)
        jobs = region_paths(d, folder, fmt, numbered)
        existing = [p for p, _a, _b in jobs if os.path.exists(p)]
        if existing and not wait and QMessageBox.question(
                self, APP_TITLE, t("rg_exists", len(existing), os.path.basename(existing[0]))) != QMessageBox.Yes:
            return []
        self.stop_play()
        total = sum(b - a for _p, a, b in jobs) or 1
        result = {}

        def work(progress):
            done = 0
            for path, a, b in jobs:
                media.save(d, path, fmt, opt, proc.SubProgress(progress, done / total, (done + b - a) / total), a, b)
                done += b - a
            return [p for p, _a, _b in jobs]

        def on_done(paths, err):
            if err:
                if err != "cancel":
                    QMessageBox.warning(self, APP_TITLE, t("op_failed", err))
                return
            result["paths"] = paths
            self._toast(t("rg_saved", len(paths), folder))
        self.run_job(t("rg_saving", len(jobs)), work, on_done, d)
        if wait:
            while self.job is not None:
                QApplication.processEvents()
                time.sleep(0.01)
        return result.get("paths", [p for p, _a, _b in jobs])

    # ── analysis: spectrum, spectrogram (0.4) ─────────────────
    def show_spectrum_panel(self, on=None) -> None:
        on = not self.spec_panel.isVisible() if on is None else bool(on)
        self.spec_panel.setVisible(on)
        self.cfg["spectrum_panel_on"] = on
        self.a_spec_panel.setChecked(on)
        self.tb["spectrum"].setChecked(on)
        if on:
            self.spec_panel.request()

    def spectrum_dialog(self) -> None:
        from .anaui import SpectrumDialog
        if self.spectrum_dlg is None:
            self.spectrum_dlg = SpectrumDialog(self)
        self.spectrum_dlg.show()
        self.spectrum_dlg.raise_()
        self.spectrum_dlg.refresh()

    def set_view_mode(self, mode: str) -> None:
        self.cfg["view_mode"] = mode
        self.wave.set_mode(mode)
        for k, a in self.mode_actions.items():
            a.setChecked(k == mode)
            self.tb["mode_" + k].setChecked(k == mode)

    def _toggle_spectrogram(self) -> None:
        self.set_view_mode("wave" if self.wave.mode != "wave" else "spec")

    def spectrogram_dialog(self) -> None:
        from .anaui import SpectrogramDialog
        dlg = SpectrogramDialog(self)
        if self.wave.mode == "wave":
            self.set_view_mode("spec")
        dlg.exec()

    def set_spectrogram(self, st, save: bool = True) -> None:
        self.wave.set_sg_settings(st)
        if save:
            self.cfg["spectrogram"] = st.to_dict()
            config.save(self.cfg)

    # ── recording (0.4) ───────────────────────────────────────
    def record_toggle(self) -> None:
        """The red button: open the Record window; there, Record; while recording, Stop."""
        if self.rec_dlg is None:
            if self._busy():
                return
            from .recui import RecordDialog
            self.stop_play()
            self.rec_dlg = RecordDialog(self)
            self.rec_dlg.recorded.connect(self.record_done)
            self.rec_dlg.finished.connect(lambda _r: self._record_closed())
            self.rec_dlg.show()
        elif self.rec_dlg.recording():
            self.rec_dlg.stop()
        elif self.rec_dlg.rec.state == "ready" and self.rec_dlg.isVisible():
            self.rec_dlg.start()
        else:
            self.rec_dlg.show()
            self.rec_dlg.raise_()
        self._update_transport()

    def _record_closed(self) -> None:
        dlg, self.rec_dlg = self.rec_dlg, None
        if dlg is not None:
            dlg.deleteLater()
        self._update_transport()

    def record_done(self, info: dict) -> None:
        """The recording goes into a new file, or into the current one at the cursor / in place of the selection
        — one undo step, with the markers set while recording."""
        src = info["src"]
        n = src.frames
        mk = [(t("mk_marker_n", i + 1), p, None) for i, p in enumerate(info["markers"])]
        if info["target"] == "new" or info["doc"] is None or info["doc"] not in self.docs:
            from . import ff as _ff
            self._recorded += 1
            ch, rate = info["channels"], info["rate"]
            lay = _ff.layout_for(ch)
            d = Document(rate=rate, channels=ch, folder=os.path.dirname(src.path) if src.path else
                         self.session.folder(), labels=tuple(_ff.labels(ch, lay)), layout=lay,
                         name=t("rec_name", self._recorded))
            from .marks import to_markers
            d.markers = to_markers(mk, n)
            d.set_initial(src, "record")
            d.saved_id = -1
            self.add_doc(d)
        else:
            d = info["doc"]
            i = self.docs.index(d)
            if self.tabs.currentIndex() != i:
                self.tabs.setCurrentIndex(i)
            a, b = info["a"], info["b"]
            d.replace(a, b, src, "record", sel=(a, a + n), remap="cut", add_markers=mk)
            self._edited()
            self.wave.ensure_visible(a)
        msg = t("rec_done", fmt_time(n, info["rate"]))
        if info.get("overflows"):
            msg += " · " + t("rec_drops", info["overflows"])
        self._toast(msg)

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
        self.spec_panel.tick()

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
            self._stats_request()
            self.spec_panel.request()
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
        self._stats_request()
        self.spec_panel.request()
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
                      (self.a_zoom_sel, sel), (self.a_play, has), (self.a_play_all, has),
                      (self.a_return_video, has), *((x, has) for x in self.film_actions)):
            a.setEnabled(on and not (busy and a not in (self.a_copy, self.a_close_all)))
        for a in self.proc_actions:
            a.setEnabled(has and not busy)
        for k, a in self.fx_actions.items():
            a.setEnabled(has and not busy and (k != "take_noise" or sel))
        for key in ("fx_eq", "fx_nr"):
            self.tb[key].setEnabled(has and not busy)
        self.a_to_mono.setEnabled(has and not busy and d.channels == 2)
        self.a_to_stereo.setEnabled(has and not busy and d.channels == 1)
        self.a_stats.setEnabled(has)
        self.a_spectrum.setEnabled(has)
        nm = has and bool(d.markers)
        for k, a in self.mk_actions.items():
            a.setEnabled(has and not busy and (k not in ("prev", "next", "rename", "delete", "delete_all",
                                                         "export") or nm)
                         and (k != "region" or sel))
        self.a_save_regions.setEnabled(has and not busy and any(m.end is not None for m in d.markers))
        self.tb["marker"].setEnabled(has and not busy)
        self.tb["region"].setEnabled(has and not busy and sel)
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
        self.tb["undo"].setToolTip(keys.tip(self.a_undo))
        self.tb["redo"].setToolTip(keys.tip(self.a_redo))
        self._title()

    def _update_transport(self) -> None:
        st = self.player.state
        self.t_play.setIcon(style.icon("fa6s.stop" if st == "play" else "fa6s.play", "#ffffff"))
        has = self.doc is not None
        for b in (self.t_start, self.t_rew, self.t_play, self.t_pause, self.t_stop, self.t_ff, self.t_end):
            b.setEnabled(has)
        rec = self.rec_dlg is not None and self.rec_dlg.recording()
        self.t_rec.setIcon(style.icon("fa6s.square" if rec else "fa6s.circle", "#e5484d"))
        self.t_rec.setToolTip(keys.tip(self.a_record, t("rec_stop_tip")) if rec else
                              keys.tip(self.a_record))
        self.t_rec.setChecked(rec)

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

    # ── the measuring: the status bar and Tools → Statistics ──
    @staticmethod
    def stats_key(d: Document, a: int, b: int) -> tuple:
        return (d.folder, d.version, int(a), int(b))

    def stats_store(self, key, st) -> None:
        """Keep a result (one with the true peak replaces one without)."""
        old = self.stats_cache.get(key)
        if old is not None and old.has_true_peak and not st.has_true_peak:
            return
        self.stats_cache.pop(key, None)
        self.stats_cache[key] = st
        while len(self.stats_cache) > STATS_CACHE:
            self.stats_cache.pop(next(iter(self.stats_cache)))
        if key == self._stats_key:
            self._show_stats(st)

    def _stats_request(self) -> None:
        """The selection or the file changed: the peak at once (from the peak levels), RMS / LUFS / clipping
        after STATS_DELAY in the background — or from the cache."""
        d = self.doc
        if d is None or d.frames <= 0:
            self._stats_key = None
            self._stats_cancel()
            self._stats_timer.stop()
            self.st_stats.setText("")
            return
        a, b = d.sel if d.sel else (0, d.frames)
        key = self.stats_key(d, a, b)
        if key == self._stats_key:
            return
        self._stats_key = key
        st = self.stats_cache.get(key)
        if st is not None:
            self._stats_cancel()
            self._stats_timer.stop()
            self._show_stats(st)
            return
        if self._stats_run and self._stats_run[0] != key:
            self._stats_cancel()
        self._show_stats(None, float(d.peak(a, b).max()))
        self._stats_timer.start()

    def _stats_start(self) -> None:
        d = self.doc
        key = self._stats_key
        if d is None or key is None or key in self.stats_cache or (self._stats_run and self._stats_run[0] == key):
            return
        if key[:2] != (d.folder, d.version):
            return
        _f, _v, a, b = key
        prog = media.Progress()
        self._stats_run = (key, prog)

        def work():
            try:
                st = analysis.analyze(d, a, b, prog, loudness=True, true_peak=False)
            except Exception:                 # cancelled, or the file closed / edited under it
                st = None
            self.bridge.stats.emit(key, st)
        threading.Thread(target=work, daemon=True).start()

    def _stats_cancel(self) -> None:
        if self._stats_run:
            self._stats_run[1].cancel()
            self._stats_run = None

    def _on_stats(self, key, st) -> None:
        if self._stats_run and self._stats_run[0] == key:
            self._stats_run = None
        if st is not None:
            self.stats_store(key, st)

    def _show_stats(self, st, peak: float | None = None) -> None:
        from .dialogs import fmt_db
        pk = st.peak if st is not None else peak
        pend = "…"
        parts = [t("st_peak_v", fmt_db(proc.gain_to_db(pk) if pk else float("-inf"), 1)),
                 t("st_rms_v", fmt_db(analysis.to_db(st.rms), 1) if st else pend),
                 t("st_lufs_v", fmt_db(st.integrated, 1) if st else pend)]
        if st is None:
            parts.append(t("st_clip_v", pend))
        elif st.clipped:
            parts.append(f"<span style='color:{style.W_CLIP}'>{t('st_clip_v', st.clipped)}</span>")
        else:
            parts.append(t("st_clip_v", 0))
        self.st_stats.setText(" · ".join(parts))

    def statistics_dialog(self) -> None:
        d = self.doc
        if d is None:
            return
        from .dialogs import StatsDialog
        if self.stats_dlg is None:
            self.stats_dlg = StatsDialog(self)
        self.stats_dlg.show()
        self.stats_dlg.raise_()
        self.stats_dlg.refresh()

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

    # ── the listening volume (2.0) ────────────────────────────
    def _volume_changed(self) -> None:
        self.a_mute.setChecked(self.volume.muted)
        self._vol_save.start()

    def _save_volume(self) -> None:
        self.cfg["play_volume_db"] = round(self.volume.db, 1)
        self.cfg["play_muted"] = self.volume.muted
        config.save(self.cfg)

    def closeEvent(self, ev) -> None:
        if self.batch_win is not None:
            if self.batch_win._running() and \
                    QMessageBox.question(self, APP_TITLE, t("bt_quit_q")) != QMessageBox.Yes:
                ev.ignore()
                return
            if self.batch_win.runner is not None and self.batch_win.runner.running:
                self.batch_win.runner.cancel()
                self.batch_win.runner.wait(30)
            self.batch_win.close()
        if self.rec_dlg is not None:                # a recording in progress becomes a document (asked below)
            self.rec_dlg.reject()
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
        self._stats_cancel()
        if self.stats_dlg is not None:
            self.stats_dlg.close()
        if self.spectrum_dlg is not None:
            self.spectrum_dlg.close()
        self.spec_panel.cancel()
        self.wave.close_engine()
        self.player.stop()
        if self.video_win is not None:
            self.video_win.close()
        self.cfg["play_volume_db"] = round(self.volume.db, 1)
        self.cfg["play_muted"] = self.volume.muted
        self.cfg["side_split"] = self.side_split.sizes()
        self.cfg["main_split"] = self.splitter.sizes()
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
