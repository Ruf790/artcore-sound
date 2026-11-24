"""Audio CD in the window (2.0): «Rip Audio CD» — the drive, the tracks with their names (CD-TEXT, MusicBrainz,
or typed), where they go (files in any format; the editor, a file per track or the whole disc with a region per
track), secure reading and the drive's offset; «Burn Audio CD» — the tracks of the document (its regions, its
markers, or the whole file), pauses, CD-TEXT, the recorder and the disc in it, Disc-At-Once or Track-At-Once, saving
the disc as .cue + .bin, erasing a CD-RW. Both run as the window's jobs."""
from __future__ import annotations

import os
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout)

from . import APP_TITLE, config, media, style
from .cd import burn as cdb
from .cd import drive as cdd
from .cd import rip as cdr
from .i18n import t


def _head(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet("font-size: 13pt; font-weight: 700; padding-bottom: 2px;")
    return lab


def _hint(text: str = "") -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("hint")
    lab.setWordWrap(True)
    return lab


class _Sig(QObject):
    disc = Signal(object, object, str)                  # toc, cdtext, error
    found = Signal(object, str)                         # [Meta], error
    media = Signal(object, str)                         # Media, error


def _option_combo(fmt: str, current: str) -> QComboBox:
    from .dialogs import option_text
    c = QComboBox()
    f = media.FORMATS[fmt]
    for o in f.options:
        c.addItem(option_text(fmt, o), o)
    c.setCurrentIndex(max(0, c.findData(current or media.default_option(fmt, 2, 16))))
    return c


# ── ripping ───────────────────────────────────────────────────
class RipDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.cfg = win.cfg
        self.toc = None
        self.drive_info = None
        self.releases: list = []
        self.sig = _Sig()
        self.sig.disc.connect(self._disc)
        self.sig.found.connect(self._found)
        self.setWindowTitle(t("cd_rip"))
        self.setMinimumSize(820, 640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("cd_rip")))
        row = QHBoxLayout()
        row.addWidget(QLabel(t("cd_drive")))
        self.drives = QComboBox()
        self.drives.setMinimumWidth(320)
        self.drives.currentIndexChanged.connect(lambda _i: self.read_disc())
        row.addWidget(self.drives, 1)
        b = QPushButton(style.icon("fa6s.rotate"), t("cd_reread"))
        b.clicked.connect(self.read_disc)
        row.addWidget(b)
        self.b_eject = QPushButton(style.icon("fa6s.eject"), t("cd_eject"))
        self.b_eject.clicked.connect(self._eject)
        row.addWidget(self.b_eject)
        lay.addLayout(row)
        self.disc_l = _hint()
        lay.addWidget(self.disc_l)
        form = QFormLayout()
        self.album = QLineEdit()
        self.artist = QLineEdit()
        self.date = QLineEdit()
        self.date.setMaximumWidth(90)
        row = QHBoxLayout()
        row.addWidget(self.album, 1)
        self.b_mb = QPushButton(style.icon("fa6s.magnifying-glass"), t("cd_mb"))
        self.b_mb.setToolTip(t("cd_mb_tip"))
        self.b_mb.clicked.connect(self.lookup)
        row.addWidget(self.b_mb)
        form.addRow(t("cd_album"), row)
        row = QHBoxLayout()
        row.addWidget(self.artist, 1)
        row.addWidget(QLabel(t("cd_year")))
        row.addWidget(self.date)
        form.addRow(t("cd_artist"), row)
        self.release = QComboBox()
        self.release.currentIndexChanged.connect(self._release_chosen)
        self.release.hide()
        form.addRow("", self.release)
        lay.addLayout(form)
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["", "№", t("cd_col_title"), t("cd_col_artist"), t("cd_col_len")])
        self.list.setRootIsDecorated(False)
        self.list.header().setSectionResizeMode(2, QHeaderView.Stretch)
        for c in (0, 1, 3, 4):
            self.list.header().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.list.itemChanged.connect(lambda *_a: self._update())
        lay.addWidget(self.list, 1)
        form = QFormLayout()
        self.mode = QComboBox()
        for k in ("files", "tracks", "disc"):
            self.mode.addItem(t("cd_mode_" + k), k)
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.cfg.get("cd_rip_mode", "files"))))
        self.mode.currentIndexChanged.connect(lambda _i: self._update())
        form.addRow(t("cd_where"), self.mode)
        row = QHBoxLayout()
        self.fmt = QComboBox()
        for k, f in media.FORMATS.items():
            if f.key != "ac3":
                self.fmt.addItem(f.name, k)
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(self.cfg.get("cd_rip_format", "flac"))))
        self.fmt.currentIndexChanged.connect(lambda _i: self._fill_options())
        row.addWidget(self.fmt)
        self.opt_holder = QHBoxLayout()
        row.addLayout(self.opt_holder)
        self.opt = None
        row.addStretch(1)
        self.fmt_row = row
        form.addRow(t("sv_format"), row)
        row = QHBoxLayout()
        self.folder = QLineEdit(self.cfg.get("cd_rip_folder") or os.path.join(os.path.expanduser("~"), "Music"))
        row.addWidget(self.folder, 1)
        b = QPushButton(t("sv_browse"))
        b.clicked.connect(self._browse)
        row.addWidget(b)
        form.addRow(t("mc_folder"), row)
        self.tmpl = QLineEdit(self.cfg.get("cd_rip_template") or "{artist} - {album}/{n:2} - {title}")
        self.tmpl.setToolTip(t("cd_template_tip"))
        form.addRow(t("bt_template"), self.tmpl)
        row = QHBoxLayout()
        self.secure = QCheckBox(t("cd_secure"))
        self.secure.setToolTip(t("cd_secure_tip"))
        self.secure.setChecked(bool(self.cfg.get("cd_secure", True)))
        row.addWidget(self.secure)
        row.addSpacing(20)
        row.addWidget(QLabel(t("cd_offset")))
        self.offset = QSpinBox()
        self.offset.setRange(-3000, 3000)
        self.offset.setValue(int(self.cfg.get("cd_offset", 0)))
        self.offset.setToolTip(t("cd_offset_tip"))
        row.addWidget(self.offset)
        row.addStretch(1)
        form.addRow("", row)
        lay.addLayout(form)
        self.status = _hint()
        lay.addWidget(self.status)
        row = QHBoxLayout()
        row.addStretch(1)
        self.b_go = QPushButton(style.icon("fa6s.compact-disc"), t("cd_rip_go"))
        self.b_go.setObjectName("primary")
        self.b_go.clicked.connect(self.accept)
        b = QPushButton(t("cancel"))
        b.clicked.connect(self.reject)
        row.addWidget(self.b_go)
        row.addWidget(b)
        lay.addLayout(row)
        self._fill_options()
        self.fill_drives()

    # ── the drive and the disc ────────────────────────────────
    def fill_drives(self) -> None:
        self.drives.blockSignals(True)
        self.drives.clear()
        for d in cdd.drives():
            self.drives.addItem(style.icon("fa6s.compact-disc"), d.name, d)
        self.drives.blockSignals(False)
        if not self.drives.count():
            self.disc_l.setText(t("cd_no_drives"))
            self._update()
            return
        last = self.cfg.get("cd_drive", "")
        for i in range(self.drives.count()):
            if self.drives.itemData(i).path == last:
                self.drives.setCurrentIndex(i)
        self.read_disc()

    def read_disc(self) -> None:
        info = self.drives.currentData()
        self.toc = None
        self.list.clear()
        self.release.hide()
        if info is None:
            return
        self.drive_info = info
        self.disc_l.setText(t("cd_reading"))
        self._update()

        def work():
            try:
                with cdd.open_drive(info) as dr:
                    toc = dr.read_toc()
                    text = dr.read_cdtext()
                self.sig.disc.emit(toc, text, "")
            except Exception as e:                       # noqa: BLE001
                self.sig.disc.emit(None, None, str(e) or type(e).__name__)
        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def wait(self, timeout: float = 30.0) -> None:
        """Tests: until the disc (or the lookup) is read."""
        import time
        from PySide6.QtWidgets import QApplication
        end = time.monotonic() + timeout
        while time.monotonic() < end and getattr(self, "_thread", None) is not None and self._thread.is_alive():
            QApplication.processEvents()
            time.sleep(0.01)
        QApplication.processEvents()

    def _disc(self, toc, text, err: str) -> None:
        if toc is None:
            self.disc_l.setText(t("cd_no_disc", err))
            self._update()
            return
        self.toc = toc
        audio = toc.audio
        secs = sum(x.length for x in audio) / 75
        note = t("cd_disc_info", len(audio), f"{int(secs // 60)}:{int(secs % 60):02d}", toc.musicbrainz_id())
        if len(audio) < len(toc.tracks):
            note += " · " + t("cd_data_track")
        if text is not None:
            note += " · CD-TEXT"
        self.disc_l.setText(note)
        meta = cdr.Meta.from_cdtext(text, len(audio))
        self._show_meta(meta)
        if text is None and self.cfg.get("cd_mb_auto", True):
            self.lookup()
        self._update()

    def _show_meta(self, meta: cdr.Meta) -> None:
        self.album.setText(meta.album)
        self.artist.setText(meta.artist)
        self.date.setText(meta.date)
        self.list.blockSignals(True)
        self.list.clear()
        for i, tr in enumerate(self.toc.audio):
            it = QTreeWidgetItem(["", f"{tr.number:02d}", meta.title(i), (meta.tracks[i].get("artist", "")
                                  if i < len(meta.tracks) else ""), f"{tr.length // 4500}:{tr.length // 75 % 60:02d}"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
            it.setCheckState(0, Qt.Checked)
            it.setData(0, Qt.UserRole, tr.number)
            self.list.addTopLevelItem(it)
        self.list.blockSignals(False)

    def meta(self) -> cdr.Meta:
        m = cdr.Meta(self.album.text().strip(), self.artist.text().strip(), self.date.text().strip())
        for i in range(self.list.topLevelItemCount()):
            it = self.list.topLevelItem(i)
            m.tracks.append({"title": it.text(2).strip(), "artist": it.text(3).strip()})
        return m

    def chosen(self) -> list[int]:
        return [self.list.topLevelItem(i).data(0, Qt.UserRole) for i in range(self.list.topLevelItemCount())
                if self.list.topLevelItem(i).checkState(0) == Qt.Checked]

    def _eject(self) -> None:
        info = self.drives.currentData()
        if info is None:
            return
        try:
            with cdd.open_drive(info) as dr:
                dr.eject()
        except Exception as e:                           # noqa: BLE001
            self.status.setText(str(e))
        self.toc = None
        self.list.clear()
        self.disc_l.setText(t("cd_ejected"))
        self._update()

    # ── MusicBrainz ───────────────────────────────────────────
    def lookup(self) -> None:
        if self.toc is None:
            return
        self.b_mb.setEnabled(False)
        self.status.setText(t("cd_mb_asking"))
        toc = self.toc

        def work():
            from .cd import musicbrainz
            try:
                self.sig.found.emit(musicbrainz.lookup(toc), "")
            except Exception as e:                       # noqa: BLE001 — no network: said, nothing else
                self.sig.found.emit([], str(e) or type(e).__name__)
        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def _found(self, releases: list, err: str) -> None:
        self.b_mb.setEnabled(True)
        self.releases = releases
        if err:
            self.status.setText(t("cd_mb_failed", err))
            return
        if not releases:
            self.status.setText(t("cd_mb_none"))
            return
        self.status.setText(t("cd_mb_found", len(releases)))
        self.release.blockSignals(True)
        self.release.clear()
        for m in releases:
            self.release.addItem(f"{m.artist} — {m.album}" + (f"  ({m.source})" if getattr(m, "source", "") else ""))
        self.release.blockSignals(False)
        self.release.setVisible(len(releases) > 1)
        self._release_chosen(0)

    def _release_chosen(self, i: int) -> None:
        if 0 <= i < len(self.releases) and self.toc is not None:
            m = self.releases[i]
            while len(m.tracks) < len(self.toc.audio):
                m.tracks.append({"title": "", "artist": ""})
            self._show_meta(m)

    # ── where to ──────────────────────────────────────────────
    def _fill_options(self) -> None:
        if self.opt is not None:
            self.opt_holder.removeWidget(self.opt)
            self.opt.deleteLater()
        fmt = self.fmt.currentData()
        last = (self.cfg.get("cd_rip_option") or {}).get(fmt, "") if isinstance(self.cfg.get("cd_rip_option"),
                                                                                 dict) else ""
        self.opt = _option_combo(fmt, last or ("16" if fmt in ("wav", "flac") else ""))
        self.opt_holder.addWidget(self.opt)

    def _browse(self) -> None:
        p = QFileDialog.getExistingDirectory(self, t("mc_folder"), self.folder.text())
        if p:
            self.folder.setText(os.path.normpath(p))

    def _update(self) -> None:
        files = self.mode.currentData() == "files"
        for w in (self.fmt, self.opt, self.folder, self.tmpl):
            if w is not None:
                w.setEnabled(files)
        self.b_go.setEnabled(self.toc is not None and bool(self.chosen()))
        self.b_mb.setEnabled(self.toc is not None)
        self.b_eject.setEnabled(self.drives.currentData() is not None)

    def settings(self) -> dict:
        return {"drive": self.drives.currentData(), "toc": self.toc, "tracks": self.chosen(), "meta": self.meta(),
                "mode": self.mode.currentData(), "format": self.fmt.currentData(), "option": self.opt.currentData(),
                "folder": self.folder.text().strip(), "template": self.tmpl.text().strip() or "{n:2} - {title}",
                "opts": cdr.Options(self.secure.isChecked(), offset=self.offset.value())}

    def accept(self) -> None:
        c = self.cfg
        c["cd_rip_mode"] = self.mode.currentData()
        c["cd_rip_format"] = self.fmt.currentData()
        opts = c.get("cd_rip_option") if isinstance(c.get("cd_rip_option"), dict) else {}
        opts[self.fmt.currentData()] = self.opt.currentData()
        c["cd_rip_option"] = opts
        c["cd_rip_folder"] = self.folder.text().strip()
        c["cd_rip_template"] = self.tmpl.text().strip()
        c["cd_secure"] = self.secure.isChecked()
        c["cd_offset"] = self.offset.value()
        if self.drives.currentData() is not None:
            c["cd_drive"] = self.drives.currentData().path
        config.save(c)
        super().accept()


def rip_path(s: dict, i: int, number: int) -> str:
    """Where a track's file goes: the template may hold folders ({artist} - {album}/…)."""
    meta = s["meta"]
    parts = [cdr.name_for(p, meta, i, number) for p in s["template"].replace("\\", "/").split("/") if p]
    return os.path.join(s["folder"], *parts) + media.FORMATS[s["format"]].ext


def run_rip(win, s: dict) -> None:
    """The rip as the window's job; the documents into the window, a word on what was read."""
    folder = win.session.folder                         # a new working folder for each document
    meta = s["meta"]
    toc = s["toc"]
    total = len(toc.audio)
    saved: list = []

    def save(d, i, number):
        path = rip_path(s, i, number)
        cdr.save_track(d, path, s["format"], s["option"], cdr.tags_for(meta, i, number, total))
        saved.append(path)

    def work(pr):
        with cdd.open_drive(s["drive"]) as dr:
            return cdr.rip(dr, toc, s["tracks"], meta, s["opts"], folder, s["mode"], pr, None, save)

    def done(res, err):
        if err:
            if err != "cancel":
                QMessageBox.warning(win, APP_TITLE, t("cd_rip_failed", err))
            return
        docs, results = res
        for d in docs:
            win.add_doc(d)
        bad = [r for r in results if r.suspicious or r.error]
        lines = [t("cd_rip_line", f"{r.number:02d}" if r.number else "CD", r.crc, r.reread, r.suspicious)
                 + (f" — {r.error}" if r.error else "") for r in results]
        msg = t("cd_rip_done", len(results)) + (" · " + t("cd_saved_n", len(saved)) if saved else "")
        win._toast(msg)
        if bad:
            QMessageBox.warning(win, APP_TITLE, t("cd_rip_report") + "\n\n" + "\n".join(lines))
        win.cd_last_rip = (results, saved)
    win.run_job(t("cd_ripping"), work, done, None)


# ── burning ───────────────────────────────────────────────────
class BurnDialog(QDialog):
    def __init__(self, win, d):
        super().__init__(win)
        self.win, self.d, self.cfg = win, d, win.cfg
        self.sig = _Sig()
        self.sig.media.connect(self._media)
        self.media = None
        self.setWindowTitle(t("cd_burn"))
        self.setMinimumSize(820, 640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("cd_burn")))
        lay.addWidget(_hint(t("cd_burn_what", d.name)))
        form = QFormLayout()
        self.how = QComboBox()
        has_regions = any(m.is_region for m in d.markers)
        has_markers = any(not m.is_region for m in d.markers)
        for k, ok in (("regions", has_regions), ("markers", has_markers), ("whole", True)):
            if ok:
                self.how.addItem(t("cd_tracks_" + k), k)
        self.how.currentIndexChanged.connect(lambda _i: self._fill_tracks())
        form.addRow(t("cd_tracks_from"), self.how)
        meta = getattr(d, "cd_meta", None)               # a disc ripped into the editor
        self.album = QLineEdit((meta.album if meta is not None else "") or
                               (os.path.splitext(d.name)[0] if d.name else ""))
        form.addRow(t("cd_album"), self.album)
        self.artist = QLineEdit((meta.artist if meta is not None else "") or self.cfg.get("cd_burn_artist", ""))
        form.addRow(t("cd_artist"), self.artist)
        lay.addLayout(form)
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["№", t("cd_col_title"), t("cd_col_artist"), t("cd_col_len")])
        self.list.setRootIsDecorated(False)
        self.list.header().setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (0, 2, 3):
            self.list.header().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        lay.addWidget(self.list, 1)
        form = QFormLayout()
        row = QHBoxLayout()
        self.gap = QDoubleSpinBox()
        self.gap.setRange(0.0, 10.0)
        self.gap.setDecimals(1)
        self.gap.setSuffix(" " + t("cd_sec"))
        self.gap.setValue(float(self.cfg.get("cd_gap", 2.0)))
        self.gap.valueChanged.connect(lambda _v: self._check())
        row.addWidget(self.gap)
        row.addStretch(1)
        form.addRow(t("cd_gap"), row)
        row = QHBoxLayout()
        self.cdtext = QCheckBox(t("cd_cdtext"))
        self.cdtext.setChecked(bool(self.cfg.get("cd_cdtext", True)))
        self.translit = QCheckBox(t("cd_translit"))
        self.translit.setToolTip(t("cd_translit_tip"))
        self.translit.setChecked(bool(self.cfg.get("cd_translit", True)))
        row.addWidget(self.cdtext)
        row.addWidget(self.translit)
        row.addStretch(1)
        form.addRow("", row)
        row = QHBoxLayout()
        self.rec = QComboBox()
        self.rec.setMinimumWidth(300)
        self.rec.currentIndexChanged.connect(lambda _i: self.check_media())
        row.addWidget(self.rec, 1)
        b = QPushButton(style.icon("fa6s.rotate"), t("cd_reread"))
        b.clicked.connect(self.check_media)
        row.addWidget(b)
        form.addRow(t("cd_recorder"), row)
        row = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItem(t("cd_dao"), "dao")
        self.mode.addItem(t("cd_tao"), "tao")
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.cfg.get("cd_burn_mode", "dao"))))
        self.mode.currentIndexChanged.connect(lambda _i: self._check())
        row.addWidget(self.mode, 1)
        row.addWidget(QLabel(t("cd_speed")))
        self.speed = QComboBox()
        self.speed.addItem(t("cd_speed_max"), 0)
        for x in (4, 8, 16, 24, 32, 48):
            self.speed.addItem(f"{x}×", x)
        self.speed.setCurrentIndex(max(0, self.speed.findData(int(self.cfg.get("cd_speed", 0)))))
        row.addWidget(self.speed)
        form.addRow(t("cd_how"), row)
        self.eject = QCheckBox(t("cd_eject_after"))
        self.eject.setChecked(True)
        form.addRow("", self.eject)
        lay.addLayout(form)
        self.media_l = _hint()
        lay.addWidget(self.media_l)
        self.note = _hint()
        self.note.setStyleSheet("color: #e5534b;")
        lay.addWidget(self.note)
        row = QHBoxLayout()
        self.b_cue = QPushButton(style.icon("fa6s.floppy-disk"), t("cd_save_cue"))
        self.b_cue.clicked.connect(self._save_cue)
        self.b_erase = QPushButton(style.icon("fa6s.eraser"), t("cd_erase"))
        self.b_erase.clicked.connect(self._erase)
        row.addWidget(self.b_cue)
        row.addWidget(self.b_erase)
        row.addStretch(1)
        self.b_go = QPushButton(style.icon("fa6s.fire"), t("cd_burn_go"))
        self.b_go.setObjectName("primary")
        self.b_go.clicked.connect(self.accept)
        b = QPushButton(t("cancel"))
        b.clicked.connect(self.reject)
        row.addWidget(self.b_go)
        row.addWidget(b)
        lay.addLayout(row)
        self.recorders = []
        self._fill_tracks()
        self.fill_recorders()

    def _fill_tracks(self) -> None:
        specs = cdb.tracks_of(self.d, self.how.currentData())
        self.list.clear()
        for i, s in enumerate(specs):
            n = s.b - s.a
            secs = n / self.d.rate
            it = QTreeWidgetItem([f"{i + 1:02d}", s.title, "", f"{int(secs // 60)}:{int(secs % 60):02d}"])
            it.setFlags(it.flags() | Qt.ItemIsEditable)
            it.setData(0, Qt.UserRole, (s.a, s.b))
            self.list.addTopLevelItem(it)
        self._check()

    def plan(self) -> cdb.Plan:
        p = cdb.Plan(gap=self.gap.value(), album=self.album.text().strip(), performer=self.artist.text().strip(),
                     cdtext=self.cdtext.isChecked() and self.mode.currentData() == "dao",
                     translit=self.translit.isChecked())
        for i in range(self.list.topLevelItemCount()):
            it = self.list.topLevelItem(i)
            a, b = it.data(0, Qt.UserRole)
            p.tracks.append(cdb.TrackSpec(int(round(a * 44100 / self.d.rate)), int(round(b * 44100 / self.d.rate)),
                                          it.text(1).strip(), it.text(2).strip()))
        return p

    def fill_recorders(self) -> None:
        self.rec.blockSignals(True)
        self.rec.clear()
        try:
            self.recorders = cdb.recorders()
        except Exception as e:                           # noqa: BLE001
            self.recorders = []
            self.media_l.setText(t("cd_imapi_failed", str(e)))
        for r in self.recorders:
            self.rec.addItem(style.icon("fa6s.compact-disc"), r.name, r)
        self.rec.blockSignals(False)
        if not self.recorders:
            self.media_l.setText(t("cd_no_recorders"))
        self.check_media()

    def check_media(self) -> None:
        r = self.rec.currentData()
        self.media = None
        if r is None:
            self._check()
            return
        self.media_l.setText(t("cd_media_checking"))

        def work():
            try:
                self.sig.media.emit(cdb.media(r), "")
            except Exception as e:                       # noqa: BLE001
                self.sig.media.emit(None, str(e) or type(e).__name__)
        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def wait(self, timeout: float = 20.0) -> None:
        import time
        from PySide6.QtWidgets import QApplication
        end = time.monotonic() + timeout
        while time.monotonic() < end and getattr(self, "_thread", None) is not None and self._thread.is_alive():
            QApplication.processEvents()
            time.sleep(0.01)
        QApplication.processEvents()

    def _media(self, m, err: str) -> None:
        self.media = m
        if m is None:
            self.media_l.setText(t("cd_media_failed", err))
        elif not m.present:
            self.media_l.setText(t("cd_media_none"))
        else:
            self.media_l.setText(t("cd_media", m.kind, t("cd_blank") if m.blank else t("cd_not_blank"),
                                   f"{m.free / 4500:.1f}"))
        self._check()

    def _check(self) -> None:
        cap = self.media.free if self.media is not None and self.media.free else cdb.MAX_SECTORS
        probs = self.plan().problems(cap)
        m = self.media
        if m is not None and m.present and not m.blank:
            probs.append(t("cd_need_blank") if not m.rewritable else t("cd_need_erase"))
        self.note.setText("\n".join(probs))
        ok_tracks = not self.plan().problems(cap)
        self.b_go.setEnabled(ok_tracks and self.rec.currentData() is not None and m is not None and m.present
                             and m.blank)
        self.b_cue.setEnabled(ok_tracks)
        self.b_erase.setEnabled(m is not None and m.present and m.rewritable and not m.blank)
        self.translit.setEnabled(self.cdtext.isChecked())

    def _save_cue(self) -> None:
        start = os.path.join(self.cfg.get("save_dir") or os.path.expanduser("~"),
                             (self.album.text().strip() or "Audio CD") + ".cue")
        path, _f = QFileDialog.getSaveFileName(self, t("cd_save_cue"), start, "CUE (*.cue)")
        if path:
            self.remember()
            plan = self.plan()
            d = self.d
            win = self.win
            self.reject()
            win.run_job(t("cd_saving_cue"), lambda pr: cdb.save_cue(d, plan, path, win.session.folder(), pr),
                        lambda res, err: QMessageBox.warning(win, APP_TITLE, t("op_failed", err)) if err and
                        err != "cancel" else win._toast(t("saved", path)), d)

    def _erase(self) -> None:
        r = self.rec.currentData()
        if r is None or QMessageBox.question(self, APP_TITLE, t("cd_erase_q")) != QMessageBox.Yes:
            return
        win = self.win

        def done(_res, err):
            if err and err != "cancel":
                QMessageBox.warning(win, APP_TITLE, t("op_failed", err))
            else:
                win._toast(t("cd_erased"))
        self.reject()
        win.run_job(t("cd_erasing"), lambda pr: cdb.erase(r, False, pr), done, None)

    def remember(self) -> None:
        c = self.cfg
        c.update(cd_gap=self.gap.value(), cd_cdtext=self.cdtext.isChecked(), cd_translit=self.translit.isChecked(),
                 cd_burn_mode=self.mode.currentData(), cd_speed=self.speed.currentData(),
                 cd_burn_artist=self.artist.text().strip())
        config.save(c)

    def settings(self) -> dict:
        return {"plan": self.plan(), "recorder": self.rec.currentData(), "mode": self.mode.currentData(),
                "speed": self.speed.currentData(), "eject": self.eject.isChecked()}

    def accept(self) -> None:
        self.remember()
        super().accept()


def run_burn(win, d, s: dict) -> None:
    """Prepare (the image or the track files) and burn, as the window's job; the files made are deleted after."""
    folder = win.session.folder()
    plan, rec = s["plan"], s["recorder"]

    def work(pr):
        from . import proc
        made = []
        try:
            if s["mode"] == "dao":
                toc, img = cdb.make_image(d, plan, folder, proc.SubProgress(pr, 0.0, 0.25))
                made.append(img)
                cdb.burn(rec, img, toc, s["speed"], proc.SubProgress(pr, 0.25, 1.0), s["eject"])
            else:
                toc, pcm = cdb.prepare(d, plan, folder, proc.SubProgress(pr, 0.0, 0.2))
                made.append(pcm)
                files = cdb.split_tracks(pcm, toc, folder)
                made += files
                cdb.burn_tao(rec, files, s["speed"], proc.SubProgress(pr, 0.2, 1.0), s["eject"])
            return toc
        finally:
            for p in made:
                try:
                    os.remove(p)
                except OSError:
                    pass

    def done(toc, err):
        if err:
            if err != "cancel":
                text = {"raw": t("cd_err_raw"), "tao": t("cd_err_tao"), "media": t("cd_err_media")}.get(err, err)
                QMessageBox.warning(win, APP_TITLE, t("cd_burn_failed", text))
            return
        win._toast(t("cd_burned", len(toc.audio), f"{toc.leadout / 4500:.1f}"))
    win.run_job(t("cd_burning"), work, done, d)
