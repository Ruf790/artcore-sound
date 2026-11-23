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
                            delay_ms=self.delay.value(), chapters=self.chapters.currentData(), out_path=out)

    def remember(self) -> None:
        o = self.options()
        q = dict(self.cfg.get("quality") or {})
        q[o.codec] = o.option
        self.cfg.update(mode=o.mode, codec=o.codec, quality=q, chapters=o.chapters,
                        dir=os.path.dirname(self.film.text()))

    def film_path(self) -> str:
        return self.film.text()


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXT
