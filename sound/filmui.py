"""The windows of the film tools (2.0): File → Return to video."""
from __future__ import annotations

import os

from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton, QVBoxLayout, QWidget)

from . import VIDEO_EXT, ff, film
from .dialogs import _buttons, _head, _hint
from .i18n import t


class ReturnDialog(QDialog):
    """Where the sound goes: which film, replace which track or add one, codec and quality, language, title,
    default, delay, chapters, and the file written (a new one next to the film by default)."""

    def __init__(self, d, cfg: dict, parent=None):
        super().__init__(parent)
        self.d = d
        self.cfg = cfg.setdefault("return_video", {}) if isinstance(cfg.get("return_video"), dict) else {}
        cfg["return_video"] = self.cfg
        self.info: ff.FileInfo | None = None
        self.setWindowTitle(t("rv_title"))
        self.setMinimumWidth(640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("rv_title")))
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        row = QHBoxLayout()
        self.film = QLineEdit()
        self.film.setReadOnly(True)
        b = QPushButton(t("rv_pick"))
        b.clicked.connect(self._pick_film)
        row.addWidget(self.film, 1)
        row.addWidget(b)
        form.addRow(t("rv_film"), row)
        what = QWidget()
        wv = QVBoxLayout(what)
        wv.setContentsMargins(0, 0, 0, 0)
        wv.setSpacing(2)
        self.mode = QButtonGroup(self)
        self.m_replace = QRadioButton(t("rv_replace"))
        self.m_add = QRadioButton(t("rv_add"))
        self.mode.addButton(self.m_replace, 0)
        self.mode.addButton(self.m_add, 1)
        rr = QHBoxLayout()
        rr.addWidget(self.m_replace)
        self.track = QComboBox()
        rr.addWidget(self.track, 1)
        wv.addLayout(rr)
        wv.addWidget(self.m_add)
        form.addRow(t("rv_what"), what)
        cr = QHBoxLayout()
        self.codec = QComboBox()
        self.quality = QComboBox()
        cr.addWidget(self.codec, 1)
        cr.addWidget(self.quality)
        form.addRow(t("rv_codec"), cr)
        self.lang = QComboBox()
        self.lang.setEditable(True)
        for code in film.LANGS:
            self.lang.addItem(f"{code} — {t('lang_' + code)}", code)
        form.addRow(t("rv_lang"), self.lang)
        self.title = QLineEdit()
        form.addRow(t("rv_track_title"), self.title)
        self.default = QCheckBox(t("rv_default"))
        form.addRow("", self.default)
        sr = QHBoxLayout()
        self.stereo = QCheckBox(t("rv_stereo"))
        self.stereo_how = QComboBox()
        for k in ("itu", "loro", "pl2"):
            self.stereo_how.addItem(t("mx_" + k), k)
        self.stereo_how.setCurrentIndex(max(0, self.stereo_how.findData(self.cfg.get("stereo_how", "itu"))))
        self.stereo.setChecked(bool(self.cfg.get("stereo", False)) and d.channels > 2)
        self.stereo.setEnabled(d.channels > 2)
        sr.addWidget(self.stereo)
        sr.addWidget(self.stereo_how, 1)
        form.addRow("", sr)
        dr = QHBoxLayout()
        self.delay = QDoubleSpinBox()
        self.delay.setRange(-60000, 60000)
        self.delay.setDecimals(0)
        self.delay.setSingleStep(10)
        self.delay.setSuffix(" " + t("un_ms"))
        self.delay.setValue(float(self.cfg.get("delay_ms", 0)))
        self.delay_frames = QLabel("")
        self.delay_frames.setObjectName("hint")
        dr.addWidget(self.delay)
        dr.addWidget(self.delay_frames, 1)
        form.addRow(t("rv_delay"), dr)
        self.chapters = QComboBox()
        self.chapters.addItem(t("rv_ch_keep"), "keep")
        self.chapters.addItem(t("rv_ch_markers", len(d.markers)), "markers")
        self.chapters.addItem(t("rv_ch_none"), "none")
        self.chapters.setCurrentIndex(max(0, self.chapters.findData(self.cfg.get("chapters", "keep"))))
        form.addRow(t("rv_chapters"), self.chapters)
        orow = QHBoxLayout()
        self.out = QLineEdit()
        ob = QPushButton("…")
        ob.setFixedWidth(34)
        ob.clicked.connect(self._pick_out)
        orow.addWidget(self.out, 1)
        orow.addWidget(ob)
        form.addRow(t("rv_out"), orow)
        self.overwrite = QCheckBox(t("rv_overwrite"))
        form.addRow("", self.overwrite)
        lay.addLayout(form)
        self.note = _hint("")
        lay.addWidget(self.note)
        self.box = _buttons(self, t("rv_ok"))
        lay.addWidget(self.box)
        self.mode.idToggled.connect(lambda _i, on: on and self._sync())
        self.codec.currentIndexChanged.connect(lambda _i: self._fill_quality())
        self.track.currentIndexChanged.connect(lambda _i: self._track_changed())
        self.delay.valueChanged.connect(lambda _v: self._sync())
        self.overwrite.toggled.connect(lambda _on: self._sync())
        self.stereo.toggled.connect(lambda _on: self._sync())
        self._set_film(d.film.get("path") or "")

    # ── the film ──────────────────────────────────────────────
    def _pick_film(self) -> None:
        start = os.path.dirname(self.film.text()) or self.cfg.get("dir", "")
        flt = t("rv_videos") + " (" + " ".join("*" + e for e in film.VIDEO_OK) + ")"
        p, _ = QFileDialog.getOpenFileName(self, t("rv_pick"), start, flt)
        if p:
            self._set_film(p)

    def _set_film(self, path: str) -> None:
        self.info = None
        self.track.clear()
        if path and os.path.isfile(path):
            try:
                self.info = ff.probe(path)
            except Exception as e:                       # noqa: BLE001
                self.note.setText(t("rv_bad_film", str(e)))
        self.film.setText(path)
        if self.info is not None:
            want = self.d.film.get("index", -1) if self.d.film.get("path") == path else -1
            for tr in self.info.tracks:
                self.track.addItem(film.track_label(tr), tr.index)
            i = self.track.findData(want)
            self.track.setCurrentIndex(i if i >= 0 else 0)
            self.out.setText(film.default_out(path))
        has_tracks = self.info is not None and bool(self.info.tracks)
        self.m_replace.setEnabled(has_tracks)
        (self.m_replace if has_tracks and self.cfg.get("mode", "replace") == "replace" else self.m_add).setChecked(True)
        self.codec.blockSignals(True)
        self.codec.clear()
        for k in film.codecs_for(path or "x.mkv"):
            c = film.CODECS[k]
            if k == "dts" and not film.dts_available():
                continue
            self.codec.addItem(c.name + (" — " + t("rv_experimental") if c.experimental else ""), k)
        self.codec.blockSignals(False)
        self._track_changed()

    def _track_changed(self) -> None:
        tr = self._track()
        want = self.cfg.get("codec") or (tr.codec if tr is not None else "ac3")
        want = {"dca": "dts"}.get(want, want)
        if want.startswith("pcm_"):
            want = "pcm"
        i = self.codec.findData(want)
        self.codec.setCurrentIndex(i if i >= 0 else 0)
        if tr is not None:
            self.lang.setEditText(tr.lang or film.guess_lang(tr.title))
            self.title.setText(tr.title)
            self.default.setChecked(tr.default or len(self.info.tracks) == 1)
        self._fill_quality()

    def _track(self):
        if self.info is None or self.track.currentIndex() < 0:
            return None
        idx = self.track.currentData()
        return next((x for x in self.info.tracks if x.index == idx), None)

    def _fill_quality(self) -> None:
        k = self.codec.currentData() or "ac3"
        c = film.CODECS[k]
        self.quality.clear()
        for o in c.options:
            self.quality.addItem(t("rv_bits", o) if c.lossless else f"{o} kbit/s", o)
        tr = self._track()
        want = (self.cfg.get("quality") or {}).get(k)
        if not want and tr is not None and not c.lossless and tr.bitrate and tr.codec in c.probe_names:
            kb = tr.bitrate / 1000
            want = min(c.options, key=lambda o: abs(int(o) - kb))
        i = self.quality.findData(want or c.default)
        self.quality.setCurrentIndex(i if i >= 0 else 0)
        self._sync()

    def _pick_out(self) -> None:
        ext = os.path.splitext(self.film.text())[1] or ".mkv"
        p, _ = QFileDialog.getSaveFileName(self, t("rv_out"), self.out.text(), f"*{ext}")
        if p:
            if not os.path.splitext(p)[1]:
                p += ext
            self.out.setText(p)

    def _sync(self) -> None:
        k = self.codec.currentData() or "ac3"
        c = film.CODECS[k]
        fps = (self.info.fps if self.info is not None else 0) or self.d.film.get("fps") or 0
        self.delay_frames.setText(t("rv_frames", f"{self.delay.value() / 1000 * fps:+.1f}", f"{fps:.3f}") if fps
                                  else "")
        self.track.setEnabled(self.m_replace.isChecked())
        self.out.setEnabled(not self.overwrite.isChecked())
        notes = []
        ch, rate, _lay = film.out_shape(k, self.d.channels, self.d.rate)
        if ch != self.d.channels:
            notes.append(t("rv_note_channels", c.name, self.d.channels, ch))
        if rate != self.d.rate:
            notes.append(t("rv_note_rate", c.name, rate))
        self.stereo_how.setEnabled(self.stereo.isChecked() and self.stereo.isEnabled())
        if self.stereo.isChecked() and self.d.channels > 2:
            q = self.quality.currentData() or c.default
            q = q if c.lossless else film.STEREO_OPTION.get(k, q)
            notes.append(t("rv_note_stereo", c.name, t("rv_bits", q) if c.lossless else f"{q} kbit/s"))
        if c.experimental:
            notes.append(t("rv_note_dts"))
        if self.overwrite.isChecked():
            notes.append(t("rv_note_overwrite"))
        notes.append(t("rv_note_copy"))
        self.note.setText("\n".join(notes))
        ok = self.info is not None and (self.m_add.isChecked() or self.track.count() > 0)
        self.box.button(self.box.StandardButton.Ok).setEnabled(ok)

    # ── the answer ────────────────────────────────────────────
    def options(self) -> film.Options:
        k = self.codec.currentData() or "ac3"
        lang = (self.lang.currentText().strip().split(" ") or [""])[0]
        out = self.film.text() if self.overwrite.isChecked() else self.out.text().strip()
        return film.Options(mode="replace" if self.m_replace.isChecked() else "add",
                            replace_index=int(self.track.currentData() if self.track.count() else -1), codec=k,
                            option=self.quality.currentData() or "", lang=(lang or "")[:3].lower(),
                            title=self.title.text().strip(), default=self.default.isChecked(),
                            delay_ms=self.delay.value(), chapters=self.chapters.currentData(), out_path=out,
                            stereo=(self.stereo_how.currentData() or "itu")
                            if self.stereo.isChecked() and self.d.channels > 2 else "",
                            stereo_title=self._stereo_title())

    def _stereo_title(self) -> str:
        base = self.title.text().strip()
        return f"{base} ({t('rv_stereo_suffix')})" if base else t("rv_stereo_suffix")

    def remember(self) -> None:
        o = self.options()
        q = dict(self.cfg.get("quality") or {})
        q[o.codec] = o.option
        self.cfg.update(mode=o.mode, codec=o.codec, quality=q, chapters=o.chapters, stereo=self.stereo.isChecked(),
                        stereo_how=self.stereo_how.currentData(),
                        dir=os.path.dirname(self.film.text()))

    def film_path(self) -> str:
        return self.film.text()


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXT


# ── automatic sync ────────────────────────────────────────────
class AutoSyncDialog(QDialog):
    """Pick a reference (another track of the film, an open document, a file), find the offset and the speed in a
    thread, show them with the confidence; Apply does it as one undo step."""

    def __init__(self, win, d):
        import threading

        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QProgressBar
        super().__init__(win)
        self.win, self.d = win, d
        self._threading = threading
        self.result_est = None
        self.apply_now = False
        self._run = None
        self.setWindowTitle(t("sy_auto"))
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("sy_auto")))
        lay.addWidget(_hint(t("sy_auto_note")))
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        rr = QHBoxLayout()
        self.ref = QComboBox()
        b = QPushButton(t("sy_ref_file"))
        b.clicked.connect(self._pick_file)
        rr.addWidget(self.ref, 1)
        rr.addWidget(b)
        form.addRow(t("sy_ref"), rr)
        self.speeds = QCheckBox(t("sy_find_speed"))
        self.speeds.setChecked(True)
        form.addRow("", self.speeds)
        self.keep_pitch = QCheckBox(t("sy_keep_pitch"))
        form.addRow("", self.keep_pitch)
        lay.addLayout(form)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.hide()
        lay.addWidget(self.bar)
        self.res = QLabel("")
        self.res.setWordWrap(True)
        self.res.setStyleSheet("font-size: 10.5pt;")
        lay.addWidget(self.res)
        row = QHBoxLayout()
        self.b_find = QPushButton(t("sy_find"))
        self.b_find.clicked.connect(self.find)
        self.b_apply = QPushButton(t("fx_apply"))
        self.b_apply.setObjectName("primary")
        self.b_apply.setEnabled(False)
        self.b_apply.clicked.connect(self._apply)
        b_close = QPushButton(t("close"))
        b_close.clicked.connect(self.reject)
        row.addWidget(self.b_find)
        row.addStretch(1)
        row.addWidget(self.b_apply)
        row.addWidget(b_close)
        lay.addLayout(row)
        self._fill_refs()
        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self._tick)

    def _fill_refs(self, extra: tuple | None = None) -> None:
        self.ref.clear()
        fp = self.d.film.get("path")
        if fp and os.path.isfile(fp):
            try:
                info = ff.probe(fp)
                for tr in info.tracks:
                    if tr.index != self.d.film.get("index"):
                        self.ref.addItem(t("sy_ref_film", film.track_label(tr)), ("file", fp, tr.pos, info.duration))
            except Exception:                            # noqa: BLE001
                pass
        for o in self.win.docs:
            if o is not self.d and o.frames > 0:
                self.ref.addItem(t("sy_ref_doc", o.name), ("doc", o))
        if extra is not None:
            self.ref.addItem(t("sy_ref_other", os.path.basename(extra[1]), extra[2] + 1), extra)
            self.ref.setCurrentIndex(self.ref.count() - 1)
        self.b_find_enabled()

    def b_find_enabled(self) -> None:
        if hasattr(self, "b_find"):
            self.b_find.setEnabled(self.ref.count() > 0)

    def _pick_file(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, t("sy_ref_file"), os.path.dirname(self.d.film.get("path") or
                                                                                    self.d.path or ""))
        if not p:
            return
        try:
            info = ff.probe(p)
        except Exception as e:                           # noqa: BLE001
            self.res.setText(t("rv_bad_film", str(e)))
            return
        pos = 0
        if len(info.tracks) > 1:
            from .dialogs import TrackDialog
            td = TrackDialog(info, self)
            if not td.exec():
                return
            pos = td.track_pos()
        self._fill_refs(("file", p, pos, info.duration))

    def find(self) -> None:
        from . import sync
        from .media import Cancelled, Progress
        ref = self.ref.currentData()
        if ref is None or self._run is not None:
            return
        state = {"f": 0.0, "est": None, "err": ""}
        prog = Progress(lambda f: state.__setitem__("f", f))
        speeds = self.speeds.isChecked()
        d = self.d

        def go():
            try:
                a = sync.envelope_of_doc(d, prog, 0.0, 0.45)
                if ref[0] == "doc":
                    b = sync.envelope_of_doc(ref[1], prog, 0.45, 0.9)
                else:
                    b = sync.envelope_of_file(ref[1], ref[2], prog, ref[3], 0.45, 0.9)
                state["est"] = sync.estimate(a, b, speeds)
            except Cancelled:
                state["err"] = "cancel"
            except Exception as e:                       # noqa: BLE001
                state["err"] = str(e) or type(e).__name__
            state["done"] = True
        self._run = (state, prog)
        self.bar.setValue(0)
        self.bar.show()
        self.b_find.setEnabled(False)
        self.b_apply.setEnabled(False)
        self.res.setText(t("sy_working"))
        self._threading.Thread(target=go, daemon=True).start()
        self.timer.start()

    def _tick(self) -> None:
        if self._run is None:
            return
        state, _p = self._run
        self.bar.setValue(int(state["f"] * 1000))
        if not state.get("done"):
            return
        self.timer.stop()
        self._run = None
        self.bar.hide()
        self.b_find.setEnabled(True)
        if state["err"]:
            self.res.setText(t("sy_failed", state["err"]) if state["err"] != "cancel" else "")
            return
        self.show_estimate(state["est"])

    def show_estimate(self, est) -> None:
        from . import sync
        self.result_est = est
        fps = self.d.film.get("fps") or 0
        off_ms = est.offset * 1000
        frames = f" ({off_ms / 1000 * fps:+.1f} {t('sy_frames_short')})" if fps else ""
        sp = t("sy_same_speed") if abs(est.L - 1) < 1e-7 else f"{sync.speed_name(est.L)} (×{est.L:.6f})"
        lines = [t("sy_res_offset", f"{off_ms:+.1f}", frames), t("sy_res_speed", sp),
                 t("sy_res_conf", round(est.confidence * 100), est.agree, est.windows, f"{est.residual_ms:.0f}")]
        if est.confidence < 0.35:
            lines.append(t("sy_res_low"))
        self.res.setText("<br>".join(lines))
        self.b_apply.setEnabled(True)

    def _apply(self) -> None:
        self.apply_now = True
        self.accept()

    def reject(self) -> None:
        if self._run is not None:
            self._run[1].cancel()
        super().reject()


# ── channels out to mono files ────────────────────────────────
class ExtractDialog(QDialog):
    def __init__(self, d, cfg: dict, parent=None):
        super().__init__(parent)
        self.d, self.cfg = d, cfg
        o = cfg.get("extract") or {}
        self.setWindowTitle(t("ex_title"))
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("ex_title")))
        lay.addWidget(_hint(t("ex_note")))
        row = QHBoxLayout()
        self.checks = []
        for i, lab in enumerate(d.labels):
            c = QCheckBox(lab)
            c.setChecked(True)
            self.checks.append(c)
            row.addWidget(c)
        row.addStretch(1)
        lay.addLayout(row)
        form = QFormLayout()
        self.fmt = QComboBox()
        self.fmt.addItem("WAV 24 bit", ("wav", "24"))
        self.fmt.addItem("WAV 16 bit", ("wav", "16"))
        self.fmt.addItem("WAV 32 float", ("wav", "32f"))
        self.fmt.addItem("FLAC 24 bit", ("flac", "24"))
        self.fmt.setCurrentIndex(max(0, int(o.get("fmt", 0))))
        form.addRow(t("ex_format"), self.fmt)
        fr = QHBoxLayout()
        base = os.path.dirname(d.path or d.film.get("path") or "") or cfg.get("save_dir", "") or os.path.expanduser("~")
        self.folder = QLineEdit(o.get("folder") or base)
        fb = QPushButton("…")
        fb.setFixedWidth(34)
        fb.clicked.connect(self._pick)
        fr.addWidget(self.folder, 1)
        fr.addWidget(fb)
        form.addRow(t("ex_folder"), fr)
        self.stem = QLineEdit(os.path.splitext(d.name or "sound")[0])
        form.addRow(t("ex_name"), self.stem)
        lay.addLayout(form)
        self.example = _hint("")
        lay.addWidget(self.example)
        lay.addWidget(_buttons(self, t("ex_ok")))
        self.stem.textChanged.connect(lambda _s: self._sync())
        self.fmt.currentIndexChanged.connect(lambda _i: self._sync())
        self._sync()

    def _pick(self) -> None:
        p = QFileDialog.getExistingDirectory(self, t("ex_folder"), self.folder.text())
        if p:
            self.folder.setText(p)

    def _sync(self) -> None:
        jobs = self.jobs()
        self.example.setText(t("ex_example", os.path.basename(jobs[0][1])) if jobs else "")

    def format(self) -> tuple[str, str]:
        return self.fmt.currentData()

    def jobs(self) -> list[tuple[int, str]]:
        from .marks import safe_name
        ext = ".wav" if self.format()[0] == "wav" else ".flac"
        stem = safe_name(self.stem.text().strip(), "sound")
        return [(i, os.path.join(self.folder.text(), f"{stem}_{safe_name(self.d.labels[i], str(i + 1))}{ext}"))
                for i, c in enumerate(self.checks) if c.isChecked()]

    def accept(self) -> None:
        if not self.jobs():
            return
        os.makedirs(self.folder.text(), exist_ok=True)
        self.cfg["extract"] = {"fmt": self.fmt.currentIndex(), "folder": self.folder.text()}
        super().accept()


# ── six mono files into 5.1 ───────────────────────────────────
class AssembleDialog(QDialog):
    SLOTS = ("L", "R", "C", "LFE", "Ls", "Rs")

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle(t("as_title"))
        self.setMinimumWidth(640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addWidget(_head(t("as_title")))
        lay.addWidget(_hint(t("as_note")))
        b = QPushButton(t("as_pick_all"))
        b.clicked.connect(self._pick_all)
        lay.addWidget(b)
        form = QFormLayout()
        self.edits = []
        for k, name in enumerate(self.SLOTS):
            r = QHBoxLayout()
            e = QLineEdit()
            e.textChanged.connect(lambda _s: self._sync())
            pb = QPushButton("…")
            pb.setFixedWidth(34)
            pb.clicked.connect(lambda _c=False, k=k: self._pick_one(k))
            r.addWidget(e, 1)
            r.addWidget(pb)
            form.addRow(name, r)
            self.edits.append(e)
        lay.addLayout(form)
        self.box = _buttons(self, t("as_ok"))
        lay.addWidget(self.box)
        self._sync()

    def _dir(self) -> str:
        return self.cfg.get("assemble_dir", "") or os.path.expanduser("~")

    def _pick_all(self) -> None:
        from . import chans
        ps, _ = QFileDialog.getOpenFileNames(self, t("as_pick_all"), self._dir())
        if not ps:
            return
        slots = chans.guess_slots(ps)
        left = [p for p in ps if p not in slots]
        for k, p in enumerate(slots):
            if not p and left:
                p = left.pop(0)
            self.edits[k].setText(p)
        self.cfg["assemble_dir"] = os.path.dirname(ps[0])

    def _pick_one(self, k: int) -> None:
        p, _ = QFileDialog.getOpenFileName(self, self.SLOTS[k], self._dir())
        if p:
            self.edits[k].setText(p)
            self.cfg["assemble_dir"] = os.path.dirname(p)

    def paths(self) -> list[str]:
        return [e.text().strip() for e in self.edits]

    def _sync(self) -> None:
        ok = all(p and os.path.isfile(p) for p in self.paths())
        self.box.button(self.box.StandardButton.Ok).setEnabled(ok)
