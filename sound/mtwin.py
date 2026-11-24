"""The main window's multitrack commands (3.0), kept out of window.py: a project tab (its view in place of the
waveform; the meters and the marker list beside it are the master's and the project's), the Multitrack menu,
the editing, playing and recording of a project, mixdown, «Send to multitrack» and «Open clip in editor».
MainWindow mixes this in; the commands of the Edit / Markers / Transport menus come here when a project tab is
the current one."""
from __future__ import annotations

import os
import time
from dataclasses import replace

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog, QInputDialog, QLineEdit, QMenu, QMessageBox

from . import APP_TITLE, AUDIO_EXT, VIDEO_EXT, config, style
from .doc import Document, fmt_time
from .i18n import t
from .mt import media as mmedia
from .mt import projfile
from .mt.model import MASTER, Project

PROJECT_EXT = projfile.EXT


class MultitrackCommands:
    # ── set-up ────────────────────────────────────────────────
    def mt_init(self) -> None:
        self.items: list = []                   # what the tabs show, in tab order: Document or Project
        self.projects: list = []
        self.mt_engines: dict = {}              # id(project) → its live Engine
        self.mt_clip = None                     # the multitrack clipboard (Project.copy_clips)
        self.mt_rec = None                      # rec.Recorder of the current project (armed / monitored)
        self.mt_rec_project = None
        self.mt_recording = None                # {"project", "punch", "loop", "start"} while recording
        self._mt_jkeys: dict = {}
        self._mt_last = None                    # the project last current (Send to multitrack)
        self.mt_cache = mmedia.cache_root(self.cfg)
        from .mt.ui.view import MultitrackView
        self.mt_view = MultitrackView()
        v = self.mt_view
        v.edited.connect(lambda: self.mt_edited(self.proj))
        v.cursorMoved.connect(self.mt_cursor_moved)
        v.action.connect(self.mt_action)
        v.addRequested.connect(lambda k: self.mt_add_track(k))
        tl = v.timeline
        tl.seekRequested.connect(self.mt_seek)
        tl.contextRequested.connect(self.mt_context)
        tl.filesDropped.connect(self.mt_drop)
        tl.openClip.connect(self.mt_open_clip)
        tl.media_frames = lambda mid: (self.proj.media[mid].frames if self.proj and mid in self.proj.media else None)
        try:
            gb = float(self.cfg.get("mt_cache_gb", 50) or 50)
            QTimer.singleShot(5000, lambda: mmedia.clean_cache(self.mt_cache, int(gb * (1 << 30))))
        except (TypeError, ValueError):
            pass

    @property
    def proj(self) -> Project | None:
        i = self.tabs.currentIndex()
        x = self.items[i] if 0 <= i < len(self.items) else None
        return x if isinstance(x, Project) else None

    @property
    def mdoc(self):
        """What the Markers menu works on: the document or the project of the current tab."""
        return self.proj or self.doc

    def pview(self):
        return self.mt_view.timeline if self.proj is not None else self.wave

    def mt_engine(self, p):
        return self.mt_engines.get(id(p)) if p is not None else None

    def playing_this(self, x) -> bool:
        """Is the player playing this document / project?"""
        if self.player.state != "play" or x is None:
            return False
        return self.player.doc is (self.mt_engine(x) if isinstance(x, Project) else x)

    # ── the Multitrack menu ───────────────────────────────────
    def build_mt_menu(self, mb, A) -> None:
        m = mb.addMenu(t("m_multitrack"))
        self.a_mt_new = A(m, "fa6s.layer-group", t("mt_new") + "…", self.mt_new_dialog, "Ctrl+Alt+N")
        self.a_mt_open = A(m, "fa6s.folder-open", t("mt_open") + "…", self.mt_open_dialog)
        self.a_mt_save_as = A(m, "fa6s.file-export", t("mt_save_as") + "…", lambda: self.mt_save(True))
        m.addSeparator()
        self.a_mt_send = A(m, "fa6s.share-from-square", t("mt_send"), self.mt_send_to, "Ctrl+Alt+T")
        self.a_mt_back = A(m, "fa6s.arrow-turn-up", t("mt_back"), self.mt_back_to_project)
        m.addSeparator()
        tm = m.addMenu(style.icon("fa6s.plus"), t("mt_tracks"))
        self.a_mt_add = A(tm, "fa6s.plus", t("mt_add_track"), lambda: self.mt_add_track("audio"), "Ctrl+Shift+T")
        self.a_mt_add_mono = A(tm, "", t("mt_add_mono"), lambda: self.mt_add_track("audio", 1))
        self.a_mt_add_six = A(tm, "", t("mt_add_six"), lambda: self.mt_add_track("audio", 6))
        self.a_mt_add_bus = A(tm, "fa6s.code-merge", t("mt_add_bus"), lambda: self.mt_add_track("bus"),
                              "Ctrl+Shift+B")
        self.a_mt_files = A(m, "fa6s.file-import", t("mt_insert_files") + "…", self.mt_insert_dialog, "Ctrl+Shift+I")
        self.a_mt_split = A(m, "fa6s.scissors", t("mt_split"), self.mt_split, "S")
        self.a_mt_dup = A(m, "fa6s.clone", t("mt_duplicate"), self.mt_duplicate, "Ctrl+D")
        self.a_mt_clip_ed = A(m, "fa6s.pen-to-square", t("mt_open_clip"), lambda: self.mt_open_clip(None),
                              "Ctrl+Shift+O")
        m.addSeparator()
        self.a_mt_snap = A(m, "fa6s.magnet", t("mt_snap"), self.mt_toggle_snap, "N")
        self.a_mt_snap.setCheckable(True)
        self.a_mt_snap.setChecked(bool(self.cfg.get("mt_snap", True)))
        self.a_mt_env = A(m, "fa6s.bezier-curve", t("mt_env_show"), self.mt_toggle_env, "E")
        self.a_mt_mixer = A(m, "fa6s.sliders", t("mt_mixer"), self.mt_toggle_mixer, "F9")
        self.a_mt_mixer.setCheckable(True)
        self.a_mt_mixer.setChecked(True)
        m.addSeparator()
        self.a_mt_punch = A(m, "fa6s.arrows-left-right-to-line", t("mt_punch"), self.mt_toggle_punch, "P")
        self.a_mt_punch.setCheckable(True)
        self.a_mt_recopt = A(m, "fa6s.microphone", t("mt_recopt_title") + "…", self.mt_rec_options)
        self.a_mt_video = A(m, "fa6s.film", t("mt_video") + "…", self.mt_video_dialog)
        m.addSeparator()
        self.a_mt_render = A(m, "fa6s.file-audio", t("mt_mix_title") + "…", self.mt_mixdown_dialog, "Ctrl+Shift+X")
        self.mt_actions = (self.a_mt_save_as, self.a_mt_add, self.a_mt_add_mono, self.a_mt_add_six,
                           self.a_mt_add_bus, self.a_mt_files, self.a_mt_split, self.a_mt_dup, self.a_mt_clip_ed,
                           self.a_mt_env, self.a_mt_mixer, self.a_mt_punch, self.a_mt_video, self.a_mt_render)

    def mt_update_actions(self) -> None:
        p = self.proj
        busy = self.job is not None
        for a in self.mt_actions:
            a.setEnabled(p is not None and not busy)
        self.a_mt_send.setEnabled(self.doc is not None and not busy and self.doc.frames > 0)
        self.a_mt_back.setEnabled(self.doc is not None and bool(getattr(self.doc, "mt_link", None)))
        self.a_mt_new.setEnabled(not busy)
        self.a_mt_open.setEnabled(not busy)
        if p is None:
            return
        sel = bool(p.sel) or bool(p.sel_clips)
        self.a_mt_clip_ed.setEnabled(not busy and len(p.sel_clips) == 1)
        self.a_mt_dup.setEnabled(not busy and bool(p.sel_clips))
        for a, on in ((self.a_save, True), (self.a_save_as, True), (self.a_close, True), (self.a_close_all, True),
                      (self.a_show, bool(p.path)), (self.a_undo, p.can_undo()), (self.a_redo, p.can_redo()),
                      (self.a_cut, sel), (self.a_copy, sel), (self.a_delete, sel),
                      (self.a_paste, self.mt_clip is not None), (self.a_sel_all, True), (self.a_zoom_sel, bool(p.sel)),
                      (self.a_play, True), (self.a_play_all, True)):
            a.setEnabled(on and not busy)
        for k, a in self.mk_actions.items():
            a.setEnabled(not busy and (k not in ("prev", "next", "rename", "delete", "delete_all", "export")
                                       or bool(p.markers)) and (k != "region" or bool(p.sel)))
        self.a_save_regions.setEnabled(False)
        self.tb["marker"].setEnabled(not busy)
        self.tb["region"].setEnabled(not busy and bool(p.sel))
        self.a_undo.setText(t("undo_n", p.undo_label()) if p.can_undo() else t("undo"))
        self.a_redo.setText(t("redo_n", p.redo_label()) if p.can_redo() else t("redo"))
        for key, act in (("save", self.a_save), ("undo", self.a_undo), ("redo", self.a_redo), ("cut", self.a_cut),
                         ("copy", self.a_copy), ("paste", self.a_paste), ("delete", self.a_delete)):
            self.tb[key].setEnabled(act.isEnabled())
        for key in ("zoom_in", "zoom_out", "zoom_all"):
            self.tb[key].setEnabled(True)
        self.a_mt_snap.setChecked(bool(p.ui.get("snap", True)))
        self.a_mt_mixer.setChecked(bool(p.ui.get("mixer", True)))
        self.tb["undo"].setToolTip(self._key_tip(self.a_undo))
        self.tb["redo"].setToolTip(self._key_tip(self.a_redo))

    @staticmethod
    def _key_tip(a) -> str:
        from . import keys
        return keys.tip(a)

    # ── tabs ──────────────────────────────────────────────────
    def add_project(self, p: Project) -> None:
        from .mt.engine import Engine
        p.journal_folder = self.session.folder()
        p.ui.setdefault("snap", bool(self.cfg.get("mt_snap", True)))
        if p.tracks and p.ui.get("current") not in {x.id for x in p.tracks}:
            p.ui["current"] = p.tracks[0].id
        self.projects.append(p)
        self.mt_engines[id(p)] = Engine(p, live=True)
        self.items.append(p)
        i = self.tabs.addTab(style.icon("fa6s.layer-group"), p.name)
        self.tabs.setTabToolTip(i, p.path or p.name)
        self.tabs.show()
        self.tabs.setCurrentIndex(i)
        self._on_tab(i)

    def mt_show(self, p: Project | None) -> None:
        """The current tab became this project (or a document: None)."""
        if p is None:
            self.center_stack.setCurrentIndex(0)
            return
        self._mt_last = p
        self.center_stack.setCurrentIndex(1)
        self.overview.hide()
        self.mt_view.set_project(p)
        if p.spp <= 0:
            QTimer.singleShot(0, self.mt_view.timeline.zoom_all)
        self.meters.set_channels(p.labels)
        self.meters.reset()
        self.mlist.fill(p, force=True)

    def mt_close(self, i: int, p: Project) -> bool:
        if self.mt_recording is not None and self.mt_recording["project"] is p:
            self.mt_record_stop()
        if p.dirty and not self._mt_ask_save(p, i):
            return False
        self.stop_play()
        if self.mt_rec_project is p:
            self._mt_close_rec()
        self.items.pop(i)
        self.projects.remove(p)
        self.tabs.blockSignals(True)
        self.tabs.removeTab(i)
        self.tabs.blockSignals(False)
        eng = self.mt_engines.pop(id(p), None)
        if eng is not None:
            eng.close()
        projfile.remove_journal(p)
        for m in p.media.values():
            m.close()
        if self._mt_last is p:
            self._mt_last = None
        if not self.items:
            self.tabs.hide()
        self._on_tab(self.tabs.currentIndex())
        return True

    def _mt_ask_save(self, p: Project, i: int) -> bool:
        self.tabs.setCurrentIndex(i)
        box = QMessageBox(QMessageBox.Question, APP_TITLE, t("mt_ask_save", p.name), parent=self)
        save = box.addButton(t("save"), QMessageBox.AcceptRole)
        nosave = box.addButton(t("dont_save"), QMessageBox.DestructiveRole)
        box.addButton(t("cancel"), QMessageBox.RejectRole)
        box.setDefaultButton(save)
        box.exec()
        b = box.clickedButton()
        if b is nosave:
            return True
        if b is save:
            return self.mt_save()
        return False

    # ── new / open / save ─────────────────────────────────────
    def mt_new_dialog(self) -> None:
        from .mt.ui.dialogs import NewProjectDialog
        dlg = NewProjectDialog(self.cfg, self)
        if not dlg.exec():
            return
        config.save(self.cfg)
        path = dlg.project_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            p = Project(dlg.rate.currentData(), dlg.chans.currentData(), os.path.splitext(os.path.basename(path))[0],
                        path, os.path.dirname(path))
            for i in range(dlg.tracks.value()):
                p.add_track(t("mt_track_n", i + 1), "audio", 2, label="new")
            p.history = p.history[-1:]
            p.pos = 0
            os.makedirs(projfile.audio_dir(p), exist_ok=True)
            projfile.save(p)
        except OSError as e:
            QMessageBox.warning(self, APP_TITLE, t("mt_create_failed", path, str(e)))
            return
        self.add_project(p)

    def mt_open_dialog(self) -> None:
        path, _f = QFileDialog.getOpenFileName(self, t("mt_open"), self.cfg.get("mt_dir") or "",
                                               f"{t('mt_files')} (*{PROJECT_EXT})")
        if path:
            self.mt_open(path)

    def mt_open(self, path: str) -> None:
        for i, x in enumerate(self.items):
            if isinstance(x, Project) and x.path and os.path.normcase(os.path.abspath(x.path)) == \
                    os.path.normcase(os.path.abspath(path)):
                self.tabs.setCurrentIndex(i)
                return
        try:
            p = projfile.load(path)
        except (OSError, ValueError, KeyError, TypeError) as e:
            QMessageBox.warning(self, APP_TITLE, t("mt_open_failed", os.path.basename(path), str(e)))
            QTimer.singleShot(0, self._open_next)
            return
        config.add_recent(self.cfg, path)
        config.save(self.cfg)

        def work(progress):
            return projfile.load_media(p, self.mt_cache, progress)

        def done(offline, err):
            if err:
                for m in p.media.values():
                    m.close()
                if err != "cancel":
                    QMessageBox.warning(self, APP_TITLE, t("mt_open_failed", os.path.basename(path), err))
            else:
                self.add_project(p)
                if offline:
                    QMessageBox.warning(self, APP_TITLE, t("mt_offline_list", "\n".join(offline[:20])))
            QTimer.singleShot(0, self._open_next)
        self.run_job(t("mt_opening", os.path.basename(path)), work, done, None)

    def mt_save(self, as_: bool = False) -> bool:
        p = self.proj
        if p is None:
            return False
        path = p.path
        if as_ or not path:
            start = path or os.path.join(p.folder or os.path.expanduser("~"), (p.name or "Project") + PROJECT_EXT)
            path, _f = QFileDialog.getSaveFileName(self, t("mt_save_as"), start, f"{t('mt_files')} (*{PROJECT_EXT})")
            if not path:
                return False
            if not path.lower().endswith(PROJECT_EXT):
                path += PROJECT_EXT
        try:
            projfile.save(p, path)
        except OSError as e:
            QMessageBox.warning(self, APP_TITLE, t("save_failed", os.path.basename(path), str(e)))
            return False
        config.add_recent(self.cfg, path)
        config.save(self.cfg)
        i = self.items.index(p)
        self.tabs.setTabToolTip(i, p.path)
        self._toast(t("mt_saved", os.path.basename(path)))
        self._update_actions()
        return True

    # ── edits ─────────────────────────────────────────────────
    def mt_edited(self, p) -> None:
        """After any change of a project (an undo step, undo / redo)."""
        if p is None:
            return
        eng = self.mt_engine(p)
        if eng is not None:
            eng.changed = True
        if p is self.proj:
            self.mt_view.sync()
            self.mlist.fill(p)
        self._update_actions()
        self.mt_cursor_moved()

    def mt_cursor_moved(self) -> None:
        p = self.proj
        if p is None:
            return
        self._mt_fields()

    def _mt_fields(self) -> None:
        p = self.proj
        if p is None:
            return
        if self.player.state != "play":
            self.counter.setText(fmt_time(p.cursor, p.rate))
        if p.sel:
            a, b = p.sel
            for f, v in ((self.f_start, a), (self.f_end, b), (self.f_len, b - a)):
                f.setText(fmt_time(v, p.rate))
        else:
            for f in (self.f_start, self.f_end, self.f_len):
                f.setText("")
        chans = t("ch_stereo") if p.channels == 2 else "5.1"
        self.fmt_label.setText(t("mt_fmt_line", f"{p.rate:,}".replace(",", " "), chans, len(p.tracks)) + " · " +
                               fmt_time(p.frames, p.rate))
        self.fmt_label.show()
        n = len(p.sel_clips)
        self.st_sel.setText(t("mt_sel_clips", n) if n else "")
        self.st_stats.setText("")
        self._update_actions()

    def mt_undo(self) -> None:
        p = self.proj
        if p is not None and p.undo():
            self.mt_edited(p)

    def mt_redo(self) -> None:
        p = self.proj
        if p is not None and p.redo():
            self.mt_edited(p)

    def mt_goto_state(self, i: int) -> None:
        p = self.proj
        if p is not None:
            p.goto_state(i)
            self.mt_edited(p)

    def _scope(self, p) -> set | None:
        st = set(p.ui.get("sel_tracks") or [])
        return st or None

    def mt_copy(self) -> bool:
        p = self.proj
        if p is None:
            return False
        clip = p.copy_clips(p.sel_clips) if p.sel_clips else (p.copy_range(*p.sel, self._scope(p)) if p.sel else
                                                                None)
        if clip is not None:
            clip["project"] = p
            self.mt_clip = clip
            self._update_actions()
            return True
        return False

    def mt_cut(self) -> None:
        p = self.proj
        if p is None or not self.mt_copy():
            return
        if p.sel_clips:
            p.delete_clips(p.sel_clips, t("cut"))
        elif p.sel:
            p.delete_range(*p.sel, self._scope(p), label=t("cut"))
        self.mt_edited(p)

    def mt_delete(self) -> None:
        p = self.proj
        if p is None:
            return
        if p.sel_clips:
            p.delete_clips(p.sel_clips, t("delete"))
        elif p.sel:
            p.delete_range(*p.sel, self._scope(p), label=t("delete"))
        else:
            return
        self.mt_edited(p)

    def mt_paste(self) -> None:
        p = self.proj
        clip = self.mt_clip
        if p is None or clip is None:
            return
        src = clip.get("project")
        if src is not p:                          # from another project: its media come along
            if clip["rate"] != p.rate:
                QMessageBox.warning(self, APP_TITLE, t("mt_paste_rate", clip["rate"], p.rate))
                return
            for mid in clip["media"]:
                if mid not in p.media and src is not None and mid in src.media:
                    m0 = src.media[mid]
                    m = mmedia.Media(mid, m0.path, m0.track, m0.name)
                    m.src, m.frames, m.channels, m.rate, m.labels = m0.src, m0.frames, m0.channels, m0.rate, m0.labels
                    p.media[mid] = m
        tid = p.ui.get("current")
        if p.track(tid) is None or p.track(tid).kind != "audio":
            audio = p.audio_tracks()
            if not audio:
                p.add_track(t("mt_track_n", 1))
                audio = p.audio_tracks()
            tid = audio[0].id
        p.paste_clips(clip, p.cursor, tid, t("paste"))
        self.mt_edited(p)

    def mt_select_all(self) -> None:
        p = self.proj
        if p is None:
            return
        p.sel_clips = frozenset(c.id for tr in p.tracks for c in tr.clips)
        p.sel = (0, p.frames) if p.frames else None
        p.ui["sel_tracks"] = []
        self.mt_view.sync()
        self.mt_cursor_moved()

    def mt_select_none(self) -> None:
        p = self.proj
        if p is None:
            return
        p.sel_clips = frozenset()
        p.sel = None
        self.mt_view.sync()
        self.mt_cursor_moved()

    def mt_split(self) -> None:
        p = self.proj
        if p is None:
            return
        at = self.player.position() if self.playing_this(p) else p.cursor
        ids = p.sel_clips or None
        if ids is None and p.ui.get("sel_tracks"):
            ids = {c.id for tr in p.tracks if tr.id in p.ui["sel_tracks"] for c in tr.clips}
        if p.split_clips(at, ids, t("mt_split")):
            self.mt_edited(p)

    def mt_duplicate(self) -> None:
        p = self.proj
        if p is None or not p.sel_clips:
            return
        found = [c for tr in p.tracks for c in tr.clips if c.id in p.sel_clips]
        span = max(c.end for c in found) - min(c.start for c in found)
        p.move_clips(p.sel_clips, span, 0, t("mt_duplicate"), copy=True)
        self.mt_edited(p)

    def mt_toggle_snap(self) -> None:
        on = self.a_mt_snap.isChecked()
        self.cfg["mt_snap"] = on
        if self.proj is not None:
            self.proj.ui["snap"] = on

    def mt_toggle_env(self) -> None:
        p = self.proj
        tid = p.ui.get("current") if p else None
        if p is None or p.track(tid) is None:
            return
        env = set(p.ui.get("env") or [])
        env.symmetric_difference_update({tid})
        p.ui["env"] = sorted(env)
        self.mt_view.sync()

    def mt_toggle_mixer(self) -> None:
        if self.proj is not None:
            self.mt_view.show_mixer(self.a_mt_mixer.isChecked())

    def mt_toggle_punch(self) -> None:
        if self.proj is not None:
            self.proj.ui["punch"] = self.a_mt_punch.isChecked()

    def mt_zoom(self, factor: float | None) -> None:
        tl = self.mt_view.timeline
        if factor is None:
            tl.zoom_all()
        elif factor == 0:
            p = self.proj
            if p is not None and p.sel:
                tl.zoom_range(*p.sel)
        else:
            tl.zoom(factor)

    # ── tracks ────────────────────────────────────────────────
    def mt_add_track(self, kind: str = "audio", channels: int = 2) -> None:
        p = self.proj
        if p is None:
            return
        n = sum(1 for x in p.tracks if x.kind == kind) + 1
        name = t("mt_track_n", n) if kind == "audio" else t("mt_bus_n", n)
        cur = p.index(p.ui.get("current")) if p.ui.get("current") is not None else -1
        tr = p.add_track(name, kind, channels, at=cur + 1 if cur >= 0 else None, label=t("mt_add_track") if
                         kind == "audio" else t("mt_add_bus"))
        p.ui["current"] = tr.id
        self.mt_edited(p)
        self.mt_view.timeline.ensure_track_visible(tr.id)

    def mt_action(self, tid: int, what: str) -> None:
        p = self.proj
        if p is None:
            return
        if what == "fx":
            self.mt_chain_dialog(tid)
        elif what.startswith("fx_edit:"):
            self.mt_edit_slot(p, tid, int(what.split(":", 1)[1]), self)
        elif what == "menu":
            self.mt_track_menu(tid)
        elif what == "send_menu":
            self.mt_send_menu(tid)
        elif what in ("arm_on", "arm_off", "mon_on", "mon_off"):
            key = "armed" if what.startswith("arm") else "monitor"
            s = set(p.ui.get(key) or [])
            (s.add if what.endswith("_on") else s.discard)(tid)
            p.ui[key] = sorted(s)
            self._mt_inputs(p)

    def mt_track_menu(self, tid: int) -> None:
        from PySide6.QtGui import QCursor
        p = self.proj
        tr = p.track(tid) if p else None
        if tr is None:
            return
        m = QMenu(self)
        m.addAction(style.icon("fa6s.pen"), t("mt_rename")).triggered.connect(lambda: self._mt_rename(tid))
        cm = m.addMenu(style.icon("fa6s.palette"), t("mt_color"))
        from .mt.ui.timeline import COLORS, track_color
        for i in range(len(COLORS)):
            from PySide6.QtGui import QIcon, QPixmap
            pm = QPixmap(14, 14)
            pm.fill(track_color(i))
            cm.addAction(QIcon(pm), str(i + 1)).triggered.connect(
                lambda _c=False, i=i: (p.set_track(tid, t("mt_color"), color=i), self.mt_edited(p)))
        if tr.kind == "audio":
            chm = m.addMenu(t("mt_channels"))
            for n, text in ((1, t("ch_mono")), (2, t("ch_stereo")), (6, "5.1")):
                a = chm.addAction(text)
                a.setCheckable(True)
                a.setChecked(tr.channels == n)
                a.triggered.connect(lambda _c=False, n=n: (p.set_track(tid, t("mt_channels"), channels=n),
                                                           self.mt_edited(p)))
            m.addAction(style.icon("fa6s.microphone"), t("mt_input") + "…").triggered.connect(
                lambda: self._mt_input(tid))
        om = m.addMenu(style.icon("fa6s.right-from-bracket"), t("mt_output"))
        for bid, name in [(MASTER, t("mt_master"))] + [(b.id, b.name) for b in p.buses() if b.id != tid]:
            a = om.addAction(name)
            a.setCheckable(True)
            a.setChecked(tr.output == bid)
            a.setEnabled(bid == MASTER or p.can_send(tid, bid))
            a.triggered.connect(lambda _c=False, b=bid: (p.set_track(tid, t("mt_output"), output=b), self.mt_edited(p)))
        m.addAction(style.icon("fa6s.wand-magic-sparkles"), t("mt_fx") + "…").triggered.connect(
            lambda: self.mt_chain_dialog(tid))
        m.addSeparator()
        env = tid in (p.ui.get("env") or [])
        a = m.addAction(style.icon("fa6s.bezier-curve"), t("mt_env_show"))
        a.setCheckable(True)
        a.setChecked(env)
        a.triggered.connect(lambda _c=False: (p.ui.__setitem__("env", sorted(set(p.ui.get("env") or [])
                                                                          ^ {tid})), self.mt_view.sync()))
        a = m.addAction(t("mt_env_play"))
        a.setCheckable(True)
        a.setChecked(tr.env_on)
        a.triggered.connect(lambda on: (p.set_track(tid, t("mt_env_play"), env_on=on), self.mt_edited(p)))
        m.addAction(t("mt_env_clear")).triggered.connect(
            lambda: (p.set_track(tid, t("mt_env_clear"), vol_env=(), pan_env=()), self.mt_edited(p)))
        m.addSeparator()
        if tr.kind == "audio":
            m.addAction(style.icon("fa6s.file-audio"), t("mt_bounce")).triggered.connect(
                lambda: self.mt_bounce(tid))
        m.addAction(style.icon("fa6s.clone"), t("mt_dup_track")).triggered.connect(
            lambda: (p.duplicate_track(tid, t("mt_dup_track")), self.mt_edited(p)))
        i = p.index(tid)
        m.addAction(style.icon("fa6s.arrow-up"), t("mt_track_up")).triggered.connect(
            lambda: (p.move_track(tid, i - 1, t("mt_track_up")), self.mt_edited(p)))
        m.addAction(style.icon("fa6s.arrow-down"), t("mt_track_down")).triggered.connect(
            lambda: (p.move_track(tid, i + 1, t("mt_track_down")), self.mt_edited(p)))
        m.addSeparator()
        m.addAction(style.icon("fa6s.trash-can"), t("mt_delete_track")).triggered.connect(
            lambda: (p.remove_tracks([tid], t("mt_delete_track")), self.mt_edited(p)))
        m.exec(QCursor.pos())

    def _mt_rename(self, tid: int) -> None:
        p = self.proj
        tr = p.track(tid) if p else None
        if tr is None:
            return
        name, ok = QInputDialog.getText(self, t("mt_rename"), t("mt_name"), QLineEdit.Normal, tr.name)
        if ok and name.strip():
            p.set_track(tid, t("mt_u_rename"), name=name.strip())
            self.mt_edited(p)

    def _mt_input(self, tid: int) -> None:
        from .mt.ui.dialogs import InputDialog
        p = self.proj
        tr = p.track(tid)
        dlg = InputDialog(tr, self)
        if dlg.exec():
            p.set_track(tid, t("mt_input"), **dlg.values())
            self.mt_edited(p)
            self._mt_inputs(p)

    def mt_send_menu(self, tid: int) -> None:
        from PySide6.QtGui import QCursor
        p = self.proj
        if p is None:
            return
        m = QMenu(self)
        buses = [b for b in p.buses() if b.id != tid]
        if not buses:
            m.addAction(t("mt_no_buses")).setEnabled(False)
        tr = p.track(tid)
        for b in buses:
            a = m.addAction(b.name)
            a.setEnabled(p.can_send(tid, b.id) and all(s.bus != b.id for s in tr.sends))
            a.triggered.connect(lambda _c=False, b=b.id: (p.set_send(tid, b, db=0.0, label=t("mt_u_send")),
                                                           self.mt_edited(p)))
        m.addSeparator()
        m.addAction(style.icon("fa6s.code-merge"), t("mt_add_bus")).triggered.connect(lambda: self.mt_add_track("bus"))
        m.exec(QCursor.pos())

    def mt_chain_dialog(self, tid: int) -> None:
        from .mt.ui.dialogs import ChainDialog
        p = self.proj
        if p is None or p.track(tid) is None:
            return
        ChainDialog(self, p, tid).exec()
        self.mt_edited(p)

    def mt_edit_slot(self, p, tid: int, sid: int, parent) -> None:
        """An effect of a chain in its own window: every change is heard at once; Cancel puts back what it was."""
        from . import fxui
        from .doc import new_document
        tr = p.track(tid)
        s = next((x for x in tr.fx if x.id == sid), None) if tr else None
        if s is None:
            return
        before = tr.fx
        d = new_document(self.session.folder(), p.rate, tr.channels if tr.kind == "audio" else p.channels)
        try:
            dlg = fxui.EffectDialog(self, s.key, d, 0, 0, True, {"values": s.values, "file": ""},
                                    plugin=(s.values or {}).get("plugin") if s.key == "vst3" else None)
            dlg.setParent(parent, dlg.windowFlags())

            def live(values, sid=sid):
                cur = p.track(tid)
                slots = tuple(replace(x, values=values) if x.id == sid else x for x in cur.fx)
                p.set_fx(tid, slots, t("mt_u_fx"), merge=f"slot:{sid}")
                eng = self.mt_engine(p)
                if eng is not None:
                    eng.changed = True
            dlg.live = live
            ok = dlg.exec()
            cur = p.track(tid)
            if ok:
                live(dlg.result_values())
                if p.history and p.history[p.pos].merge == f"slot:{sid}":
                    p.history[p.pos].merge = ""
            elif cur is not None and cur.fx != before:
                p.set_fx(tid, before, t("mt_u_fx"))
        finally:
            d.close()
        self.mt_edited(p)

    # ── clips ─────────────────────────────────────────────────
    def mt_context(self, gpos, tr, frame: int, c) -> None:
        p = self.proj
        if p is None:
            return
        m = QMenu(self)
        if c is not None:
            m.addAction(style.icon("fa6s.scissors"), t("mt_split_here")).triggered.connect(
                lambda: (p.split_clips(frame, {c.id}, t("mt_split")), self.mt_edited(p)))
            m.addAction(style.icon("fa6s.pen-to-square"), t("mt_open_clip")).triggered.connect(
                lambda: self.mt_open_clip(c.id))
            a = m.addAction(t("mt_clip_mute"))
            a.setCheckable(True)
            a.setChecked(c.muted)
            a.triggered.connect(lambda on: (p.set_clip(c.id, t("mt_clip_mute"), muted=on), self.mt_edited(p)))
            m.addAction(style.icon("fa6s.volume-high"), t("mt_clip_gain") + "…").triggered.connect(
                lambda: self._mt_clip_gain(c.id))
            for which, key in (("in", "curve_in"), ("out", "curve_out")):
                fm = m.addMenu(t("mt_fade_" + which))
                for cv in ("linear", "log", "exp", "scurve"):
                    a = fm.addAction(t("curve_" + cv))
                    a.setCheckable(True)
                    a.setChecked(getattr(c, key) == cv)
                    a.triggered.connect(lambda _c=False, k=key, cv=cv: (p.set_clip(c.id, t("mt_u_fade"), **{k: cv}),
                                                                        self.mt_edited(p)))
            if len(c.takes) > 1:
                tk = m.addMenu(style.icon("fa6s.layer-group"), t("mt_takes"))
                for i, k in enumerate(c.takes):
                    a = tk.addAction(k.name or t("mt_take_name", i + 1))
                    a.setCheckable(True)
                    a.setChecked(i == c.take)
                    a.triggered.connect(lambda _c=False, i=i: (p.set_clip(c.id, t("mt_takes"), take=i),
                                                               self.mt_edited(p)))
                tk.addSeparator()
                tk.addAction(t("mt_take_delete")).triggered.connect(
                    lambda: (p.delete_take(c.id, c.take, t("mt_take_delete")), self.mt_edited(p)))
            mm = p.media.get(c.media)
            if mm is not None and mm.path:
                m.addAction(style.icon("fa6s.folder"), t("show_file")).triggered.connect(
                    lambda: self._mt_show_path(mm.path))
            if mm is not None and mm.offline:
                m.addAction(style.icon("fa6s.link"), t("mt_relink") + "…").triggered.connect(
                    lambda: self.mt_relink(mm.id))
            m.addSeparator()
            m.addAction(style.icon("fa6s.trash-can"), t("delete")).triggered.connect(self.mt_delete)
        else:
            m.addAction(style.icon("fa6s.file-import"), t("mt_insert_files") + "…").triggered.connect(
                lambda: self.mt_insert_dialog(tr.id if tr is not None and tr.kind == "audio" else None, frame))
            if self.mt_clip is not None and tr is not None and tr.kind == "audio":
                def paste_here():
                    p.cursor = frame
                    p.ui["current"] = tr.id
                    self.mt_paste()
                m.addAction(style.icon("fa6s.paste"), t("paste")).triggered.connect(paste_here)
            m.addAction(style.icon("fa6s.plus"), t("mt_add_track")).triggered.connect(lambda: self.mt_add_track())
            if tr is not None:
                m.addAction(style.icon("fa6s.ellipsis"), t("mt_track_menu")).triggered.connect(
                    lambda: self.mt_track_menu(tr.id))
        m.exec(gpos)

    def _mt_show_path(self, path: str) -> None:
        from . import winint
        winint.show_in_explorer(path)

    def _mt_clip_gain(self, cid: int) -> None:
        p = self.proj
        _tr, c = p.clip(cid)
        if c is None:
            return
        v, ok = QInputDialog.getDouble(self, t("mt_clip_gain"), t("mt_clip_gain_db"), c.gain_db, -60, 24, 1)
        if ok:
            p.set_clip(cid, t("mt_clip_gain"), gain_db=v)
            self.mt_edited(p)

    def mt_relink(self, mid: str) -> None:
        p = self.proj
        m = p.media.get(mid) if p else None
        if m is None:
            return
        exts = " ".join(f"*{e}" for e in AUDIO_EXT + VIDEO_EXT)
        path, _f = QFileDialog.getOpenFileName(self, t("mt_relink"), os.path.dirname(m.path) if m.path else "",
                                               f"{t('files_audio')} ({exts})")
        if not path:
            return
        m.path = os.path.abspath(path)
        m.name = os.path.basename(path)

        def work(progress):
            return mmedia.load(m, p.rate, self.mt_cache, progress)

        def done(_r, err):
            if m.offline:
                QMessageBox.warning(self, APP_TITLE, t("mt_relink_failed", os.path.basename(path), m.error or err))
            p.version += 1
            self.mt_edited(p)
        self.run_job(t("mt_opening", os.path.basename(path)), work, done, None)

    def mt_insert_dialog(self, tid=None, frame=None) -> None:
        p = self.proj
        if p is None:
            return
        exts = " ".join(f"*{e}" for e in AUDIO_EXT + VIDEO_EXT)
        paths, _f = QFileDialog.getOpenFileNames(self, t("mt_insert_files"), self.cfg.get("last_dir", ""),
                                                 f"{t('files_audio')} ({exts});;{t('files_all')} (*)")
        if paths:
            self.cfg["last_dir"] = os.path.dirname(paths[0])
            cur = p.ui.get("current")
            if tid is None and p.track(cur) is not None and p.track(cur).kind == "audio":
                tid = cur
            self.mt_insert(paths, tid, p.cursor if frame is None else frame)

    def mt_drop(self, paths: list, tid, frame: int) -> None:
        projects = [x for x in paths if x.lower().endswith(PROJECT_EXT)]
        if projects:
            self.open_paths(projects)
        files = [x for x in paths if not x.lower().endswith(PROJECT_EXT)]
        if files:
            self.mt_insert(files, tid, frame)

    def mt_insert(self, paths: list, tid, frame: int, track_pos: int = 0, p=None) -> None:
        """Files onto the timeline from `frame`: the first on track tid (a new one when None), the next on the
        tracks below (made when missing). Files are decoded into the cache the first time (a job)."""
        p = p or self.proj
        if p is None or self._busy():
            return
        files = []
        for x in paths:
            if os.path.isdir(x):
                files += [os.path.join(x, n) for n in sorted(os.listdir(x)) if n.lower().endswith(AUDIO_EXT)]
            elif os.path.isfile(x):
                files.append(x)
        if not files:
            return
        ms = [mmedia.Media(mmedia.new_media_id(), f, track_pos) for f in files]

        def work(progress):
            from .proc import SubProgress
            for i, m in enumerate(ms):
                mmedia.load(m, p.rate, self.mt_cache, SubProgress(progress, i / len(ms), (i + 1) / len(ms)))
            return ms

        def done(_r, err):
            if err:
                if err != "cancel":
                    QMessageBox.warning(self, APP_TITLE, err)
                return
            bad = [m for m in ms if m.offline]
            if bad:
                QMessageBox.warning(self, APP_TITLE, t("mt_insert_failed", "\n".join(
                    f"{m.name}: {m.error}" for m in bad)))
            good = [m for m in ms if not m.offline]
            if not good:
                return
            audio = [x for x in p.tracks if x.kind == "audio"]
            idx = next((i for i, x in enumerate(audio) if x.id == tid), None)
            changes = 0
            for k, m in enumerate(good):
                p.media[m.id] = m
                j = (idx + k) if idx is not None else None
                if j is None or j >= len(audio):
                    n = len([x for x in p.tracks if x.kind == "audio"]) + 1
                    tr = p.add_track(os.path.splitext(m.name)[0] or t("mt_track_n", n), "audio",
                                     1 if m.channels == 1 else (6 if m.channels == 6 and p.channels == 6 else 2),
                                     label=t("mt_add_track"))
                    audio = [x for x in p.tracks if x.kind == "audio"]
                    idx = idx if idx is not None else len(audio) - 1 - k
                else:
                    tr = audio[j]
                p.add_clip(tr.id, m.id, frame, m.frames, 0, os.path.splitext(m.name)[0], t("mt_u_insert"))
                changes += 1
            if changes > 1:
                self._mt_squash(p, changes + (len(good) - changes), t("mt_u_insert"))
            self.mt_edited(p)
            self.mt_view.timeline.ensure_visible(frame)
        self.run_job(t("mt_inserting", len(files)), work, done, None)

    @staticmethod
    def _mt_squash(p, n: int, label: str) -> None:
        """The last n steps of the history as one."""
        n = min(n, p.pos)
        if n <= 1:
            return
        last = p.history[p.pos]
        first = p.history[p.pos - n + 1]
        last.label = label
        last.sel_before, last.cursor_before = first.sel_before, first.cursor_before
        del p.history[p.pos - n + 1:p.pos]
        p.pos -= n - 1

    # ── a clip in the editor and back ─────────────────────────
    def mt_open_clip(self, cid) -> None:
        """The clip's file in an editor tab; Save there writes a copy into the project's audio folder, and every clip
        of that file plays the copy (the original is never written over)."""
        p = self.proj
        if p is None:
            return
        if cid is None:
            if len(p.sel_clips) != 1:
                return
            cid = next(iter(p.sel_clips))
        _tr, c = p.clip(cid)
        m = p.media.get(c.media) if c else None
        if m is None or m.offline or self._busy():
            return
        for i, x in enumerate(self.items):
            if isinstance(x, Document) and getattr(x, "mt_link", None) and x.mt_link["media"] == m.id and \
                    x.mt_link["project"] is p:
                self.tabs.setCurrentIndex(i)
                return
        self.stop_play()
        folder = self.session.folder()
        src = m.src

        def work(progress):
            d = Document(rate=p.rate, channels=src.channels, folder=folder, labels=tuple(m.labels))
            w = None
            from . import store
            w = store.SourceWriter(folder, src.channels)
            try:
                for a in range(0, src.frames, store.CHUNK):
                    w.append(src.read(a, min(store.CHUNK, src.frames - a)))
                    progress(a / max(1, src.frames))
            except BaseException:
                w.abort()
                raise
            d.set_initial(w.finish(), "open")
            return d

        def done(d, err):
            if err:
                if err != "cancel":
                    QMessageBox.warning(self, APP_TITLE, err)
                return
            stem = os.path.splitext(m.name)[0]
            n = 1
            while True:
                path = os.path.join(projfile.audio_dir(p), t("mt_edit_name", stem, n) + ".wav")
                if not os.path.exists(path):
                    break
                n += 1
            d.name = os.path.basename(path)
            d.path = path
            d.save_format, d.save_opts = "wav", {"option": "32f"}
            d.mt_link = {"project": p, "media": m.id, "clip": cid}
            d.saved_id = -1
            if c is not None:
                d.sel = (c.offset, min(d.frames, c.offset + c.length))
                d.cursor = c.offset
            self.add_doc(d)
            self._toast(t("mt_clip_opened", os.path.basename(path)))
        self.run_job(t("mt_opening", m.name), work, done, None)

    def mt_after_save(self, d) -> None:
        """An edited clip was saved: its file becomes a medium of the project in place of the old one."""
        link = getattr(d, "mt_link", None)
        if not link:
            return
        p = link["project"]
        if p not in self.projects:
            d.mt_link = None
            return
        old = link["media"]
        new = mmedia.Media(mmedia.new_media_id(), d.path, 0, os.path.basename(d.path))
        try:
            mmedia.load(new, p.rate, self.mt_cache)
        except Exception as e:                       # noqa: BLE001
            new.error = str(e)
        if new.offline:
            QMessageBox.warning(self, APP_TITLE, t("mt_relink_failed", os.path.basename(d.path), new.error))
            return
        p.media[new.id] = new
        p.replace_media(old, new.id, label=t("mt_u_edit_clip"))
        link["media"] = new.id
        self.mt_edited(p)

    def mt_back_to_project(self) -> None:
        d = self.doc
        link = getattr(d, "mt_link", None) if d else None
        if not link:
            return
        if d.dirty and not self.save(wait=True):
            return
        p = link["project"]
        if p in self.items:
            self.tabs.setCurrentIndex(self.items.index(p))

    def mt_send_to(self) -> None:
        """The editor's selection (or the whole document) onto the last project's current track at its cursor."""
        d = self.doc
        if d is None or self._busy() or d.frames <= 0:
            return
        p = self._mt_last if self._mt_last in self.projects else (self.projects[-1] if self.projects else None)
        if p is None:
            if QMessageBox.question(self, APP_TITLE, t("mt_send_new")) != QMessageBox.Yes:
                return
            self.mt_new_dialog()
            p = self.proj
            if p is None:
                return
            self.tabs.setCurrentIndex(self.items.index(d))
        if d.rate != p.rate:
            QMessageBox.warning(self, APP_TITLE, t("mt_send_rate", d.rate, p.rate))
            return
        a, b = d.sel if d.sel else (0, d.frames)
        stem = os.path.splitext(d.name)[0] or "clip"
        path = mmedia.unique_path(projfile.audio_dir(p), stem + (" (" + t("mt_sel_word") + ")" if d.sel else ""))

        def work(progress):
            return mmedia.write_doc(d, a, b, path, self.mt_cache, progress)

        def done(src, err):
            if err:
                if err != "cancel":
                    QMessageBox.warning(self, APP_TITLE, t("save_failed", os.path.basename(path), err))
                return
            mmedia.remember_peaks(src, self.mt_cache)
            m = mmedia.media_from_source(src, os.path.basename(path))
            p.media[m.id] = m
            tid = p.ui.get("current")
            if p.track(tid) is None or p.track(tid).kind != "audio":
                audio = p.audio_tracks()
                tid = audio[0].id if audio else p.add_track(t("mt_track_n", 1), "audio", min(2, d.channels)).id
            p.add_clip(tid, m.id, p.cursor, m.frames, 0, stem, t("mt_send"))
            self.tabs.setCurrentIndex(self.items.index(p))
            self.mt_edited(p)
            self._toast(t("mt_sent", p.name))
        self.run_job(t("mt_sending"), work, done, d)

    def mt_bounce(self, tid: int) -> None:
        p = self.proj
        tr = p.track(tid) if p else None
        if tr is None or not tr.clips or self._busy():
            return
        self.stop_play()
        path = mmedia.unique_path(projfile.audio_dir(p), t("mt_bounce_name", tr.name))
        from .mt import render

        def work(progress):
            return render.bounce_track(p, tid, path, self.mt_cache, progress)

        def done(src, err):
            if err:
                if err != "cancel":
                    QMessageBox.warning(self, APP_TITLE, err)
                return
            mmedia.remember_peaks(src, self.mt_cache)
            m = mmedia.media_from_source(src, os.path.basename(path))
            p.media[m.id] = m
            nt = p.add_track(t("mt_bounce_name", tr.name), "audio", src.channels if src.channels in (1, 2, 6) else 2,
                             at=p.index(tid) + 1, label=t("mt_bounce"), output=tr.output, pan=tr.pan,
                             gain_db=tr.gain_db, color=tr.color)
            p.add_clip(nt.id, m.id, 0, src.frames, 0, nt.name, t("mt_bounce"), xfade=False)
            p.set_track(tid, t("mt_bounce"), mute=True)
            self._mt_squash(p, 3, t("mt_bounce"))
            self.mt_edited(p)
            self._toast(t("mt_bounced", tr.name))
        self.run_job(t("mt_bouncing", tr.name), work, done, None)

    # ── video ─────────────────────────────────────────────────
    def mt_video_dialog(self) -> None:
        p = self.proj
        if p is None:
            return
        exts = " ".join(f"*{e}" for e in VIDEO_EXT)
        path, _f = QFileDialog.getOpenFileName(self, t("mt_video"), os.path.dirname(p.video.get("path", "")) or
                                               p.folder, f"{t('mt_files_video')} ({exts})")
        if not path:
            return
        from . import ff
        try:
            info = ff.probe(path)
        except Exception as e:                       # noqa: BLE001
            QMessageBox.warning(self, APP_TITLE, t("open_failed", os.path.basename(path), str(e)))
            return
        p.video = {"path": os.path.abspath(path), "fps": info.fps or 25.0, "offset": 0}
        p.version += 1
        if info.tracks and QMessageBox.question(self, APP_TITLE, t("mt_video_sound")) == QMessageBox.Yes:
            self.mt_insert([path], None, 0, info.tracks[0].pos, p)
        if self.video_win is None:
            self.video_window_toggle()

    # ── markers on a project ──────────────────────────────────
    def mt_marker_changed(self) -> None:
        p = self.proj
        if p is not None:
            self.mlist.fill(p)
            self.mt_edited(p)

    # ── transport ─────────────────────────────────────────────
    def mt_play(self, whole: bool = False) -> None:
        p = self.proj
        eng = self.mt_engine(p)
        if p is None or eng is None or self.job:
            return
        loop = self.a_loop.isChecked()
        end = p.frames
        if whole:
            start = 0
        elif p.sel and (loop or not (p.sel[0] <= p.cursor < p.sel[1])):
            start, end = p.sel[0], p.sel[1]
        elif p.sel:
            start, end = p.cursor, p.sel[1]
        else:
            start = p.cursor if p.cursor < p.frames else 0
        if end - start <= 0:
            return
        ok = self.player.play(eng, start, end, loop and bool(p.sel) and not whole)
        if ok and p.sel and loop and start != p.sel[0]:
            self.player.start = p.sel[0]
        if not ok and self.player.error:
            self._toast(t("playback_err", self.player.error))
        self._update_transport()

    def mt_seek(self, frame: int) -> None:
        p = self.proj
        if p is not None and self.playing_this(p) and self.mt_recording is None:
            end = p.sel[1] if p.sel and p.sel[0] <= frame < p.sel[1] else p.frames
            if end > frame:
                self.player.play(self.mt_engine(p), frame, end, self.a_loop.isChecked() and bool(p.sel))
        self.mt_cursor_moved()

    def mt_stopped(self, p, pos: int, origin: int) -> None:
        p.cursor = origin if self.cfg.get("return_cursor", True) else pos
        self.mt_view.timeline.set_play_pos(None)
        self.mt_view.feed({})
        self.mt_cursor_moved()

    def mt_tick(self) -> None:
        p = self.proj
        if p is None:
            return
        pl = self.player
        tl = self.mt_view.timeline
        rec = self.mt_rec if self.mt_rec_project is p else None
        levels = rec.levels() if rec is not None else {}
        if self.playing_this(p):
            pos = pl.position()
            tl.set_play_pos(pos, self.cfg.get("follow", True))
            self.counter.setText(fmt_time(pos, p.rate))
            meters = dict(self.mt_engine(p).meters_at(pos))
            meters.update(levels)
            self.mt_view.feed(meters)
            self.meters.feed(meters.get(MASTER))
        elif levels:
            self.mt_view.feed(levels)
        if self.mt_recording is not None and rec is not None:
            tl.rec_preview = {tid: v for tid, v in rec.recorded().items()}
            tl.update()

    # ── recording ─────────────────────────────────────────────
    def _mt_inputs(self, p) -> None:
        """Open the inputs of the armed / monitored tracks (meters, monitoring), close them when none is left."""
        if self.mt_recording is not None:
            return
        armed = [x for x in p.audio_tracks() if x.id in (p.ui.get("armed") or [])]
        mon = [x for x in p.audio_tracks() if x.id in (p.ui.get("monitor") or [])]
        self._mt_close_rec()
        if not armed and not mon:
            self.mt_view.sync()
            return
        from .mt.rec import Recorder
        from .player import InputMonitor
        monitor = InputMonitor(self.cfg.get("device", ""), self.cfg.get("latency", "low"), self.volume) if mon else None
        r = Recorder(p, self.mt_cache, projfile.audio_dir(p), p.rate, monitor,
                     int(float(self.cfg.get("mt_rec_offset_ms", 0.0)) * p.rate / 1000))
        if not r.open(armed, mon):
            QMessageBox.warning(self, APP_TITLE, r.error)
            p.ui["armed"], p.ui["monitor"] = [], []
            self.mt_view.sync()
            return
        self.mt_rec, self.mt_rec_project = r, p
        self.mt_view.sync()

    def _mt_close_rec(self) -> None:
        if self.mt_rec is not None:
            self.mt_rec.close()
        self.mt_rec, self.mt_rec_project = None, None

    def mt_record_toggle(self) -> None:
        if self.mt_recording is not None:
            self.mt_record_stop()
        else:
            self.mt_record_start()

    def mt_record_start(self) -> None:
        p = self.proj
        if p is None or self._busy():
            return
        if not p.ui.get("armed"):
            self._toast(t("mt_arm_first"))
            return
        if self.mt_rec is None or self.mt_rec_project is not p:
            self._mt_inputs(p)
            if self.mt_rec is None:
                return
        self.stop_play()
        punch = tuple(p.sel) if p.ui.get("punch") and p.sel else None
        loop = bool(punch) and self.a_loop.isChecked()
        pre = int(float(self.cfg.get("mt_preroll", 2.0)) * p.rate) if punch else 0
        start = max(0, (punch[0] if punch else p.cursor) - pre)
        end = max(p.frames, start) + int(3600 * p.rate) if not loop else punch[1]
        eng = self.mt_engine(p)
        eng.extra_input = None
        eng.extend = end
        if not self.player.play(eng, start, end, loop):
            eng.extend = 0
            QMessageBox.warning(self, APP_TITLE, t("playback_err", self.player.error))
            return
        if loop:
            self.player.start = punch[0]
        rate = p.rate
        pl = self.player

        def clock(tm, pl=pl, rate=rate):
            pos, t0, _now = pl._clock
            return pos + (tm - t0) * rate

        names = {tid: t("mt_rec_file", p.track(tid).name, time.strftime("%H%M%S")) for tid in self.mt_rec.tracks}
        if not self.mt_rec.record(clock, names):
            self.player.stop()
            return
        self.mt_recording = {"project": p, "punch": punch, "loop": loop, "start": start}
        self._update_transport()

    def mt_record_stop(self) -> None:
        info, self.mt_recording = self.mt_recording, None
        if info is None:
            return
        p = info["project"]
        self.player.stop()
        eng = self.mt_engine(p)
        if eng is not None:
            eng.extend = 0
        r = self.mt_rec
        results = r.stop() if r is not None else []
        self.mt_view.timeline.rec_preview = {}
        out = []
        for tid, src, start in results:
            m = mmedia.media_from_source(src, os.path.basename(src.path))
            p.media[m.id] = m
            out.append((tid, m.id, start, src.frames))
        if out:
            from .mt.rec import place
            place(p, out, info["punch"], info["loop"], t("record"))
            drops = r.overflows() if r is not None else 0
            n = max(s for _t, _m, _s, s in out)
            msg = t("rec_done", fmt_time(n, p.rate))
            if drops:
                msg += " · " + t("rec_drops", drops)
            self._toast(msg)
        self.mt_edited(p)
        self.mt_stopped(p, p.cursor, info["start"] if not info["punch"] else info["punch"][0])
        self._update_transport()

    def mt_rec_options(self) -> None:
        from .mt.ui.dialogs import RecOptionsDialog
        dlg = RecOptionsDialog(self.cfg, self)
        if dlg.exec():
            config.save(self.cfg)

    # ── the mixdown ───────────────────────────────────────────
    def mt_mixdown_dialog(self) -> None:
        p = self.proj
        if p is None or self._busy():
            return
        from .mt import render
        from .mt.ui.dialogs import MixdownDialog
        dlg = MixdownDialog(p, self.cfg, self)
        if not dlg.exec():
            return
        config.save(self.cfg)
        kind, fmt, opt, target, tail = dlg.kind(), dlg.format(), dlg.option(), dlg.target(), dlg.tail.isChecked()
        if kind in ("whole", "sel") and os.path.exists(target) and \
                QMessageBox.question(self, APP_TITLE, t("sv_exists", os.path.basename(target))) != QMessageBox.Yes:
            return
        self.stop_play()
        a, b = (p.sel if kind == "sel" else (0, p.frames))
        ids = dlg.stem_ids()

        def work(progress):
            if kind in ("whole", "sel"):
                render.mixdown(p, target, fmt, opt, a, b, progress, tail)
                return [target]
            if kind == "stems":
                return render.stems(p, target, fmt, opt, ids, 0, p.frames, progress, tail)
            if kind == "regions":
                return render.regions(p, target, fmt, opt, progress, True, tail)
            return render.to_document(p, self.session.folder(), a, b, progress, tail)

        def done(res, err):
            if err:
                if err != "cancel":
                    QMessageBox.warning(self, APP_TITLE, t("mt_mix_failed", err))
                return
            if kind == "tab":
                self.add_doc(res)
                return
            self._toast(t("mt_mix_done", len(res), os.path.basename(res[0]) if len(res) == 1 else
                          os.path.dirname(res[0])))
            self.last_mix = res
        self.run_job(t("mt_mixing"), work, done, None)

    # ── journals ──────────────────────────────────────────────
    def mt_write_journals(self, force: bool = False) -> None:
        for p in list(self.projects):
            key = projfile.journal_key(p)
            if not force and self._mt_jkeys.get(id(p)) == key:
                continue
            try:
                projfile.write_journal(p)
                self._mt_jkeys[id(p)] = key
            except (OSError, ValueError, TypeError):
                pass

    def mt_restore(self, e) -> Project:
        """A project of a crashed run (recovery.Entry of kind "project"), its media loaded again."""
        self.session.adopt(e.session)
        p = projfile.from_journal(e.data)
        projfile.load_media(p, self.mt_cache)
        self.add_project(p)
        return p
