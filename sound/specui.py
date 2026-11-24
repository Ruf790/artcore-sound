"""The window of spectral editing (2.0): the tools that draw on the spectrogram (time / rectangle / brush / lasso), the
brush's size, what is done inside the selection (attenuate, erase, fill from the neighbours, boost), the softness of
its edges, listening (the selection only / the result / the original — a piece rendered ahead in a thread, played
in a loop) and Apply as one undo step. It stays open beside the wave (not modal)."""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QSlider, QVBoxLayout)

from . import spectral, style
from .dialogs import _head, _hint
from .doc import fmt_time
from .i18n import t

TOOLS = (("time", "fa6s.i-cursor"), ("rect", "fa6s.vector-square"), ("brush", "fa6s.paintbrush"),
         ("lasso", "fa6s.draw-polygon"))
PREVIEW_S = 15.0                    # the longest piece the preview renders


def fmt_hz(f: float) -> str:
    return f"{f / 1000:.2f} {t('un_kHz')}" if f >= 1000 else f"{f:.0f} {t('un_Hz')}"


class SpectralPanel(QDialog):
    rendered = Signal(object, str, int)              # (document piece or None, error, run)

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle(t("se_title"))
        self.setModal(False)
        self.setMinimumWidth(460)
        # the chosen tool and what is being heard stand out (the theme leaves checked push buttons plain)
        self.setStyleSheet(f"QPushButton:checked {{ background: {style.ACCENT}; color: #ffffff; "
                           f"border-color: {style.ACCENT}; }}")
        cfg = win.cfg.setdefault("spectral", {}) if isinstance(win.cfg.get("spectral"), dict) else {}
        win.cfg["spectral"] = cfg
        self.cfg = cfg
        self._run = 0
        self._piece = None                         # the rendered Document being played
        self._listen = ""                          # what is being heard: selection | result | original
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 12)
        lay.setSpacing(8)
        lay.addWidget(_head(t("se_title")))
        row = QHBoxLayout()
        self.tools = QButtonGroup(self)
        self.tool_buttons = {}
        for k, icon in TOOLS:
            b = QPushButton(style.icon(icon), t("se_tool_" + k))
            b.setToolTip(t("se_tool_" + k))
            b.setCheckable(True)
            self.tools.addButton(b)
            self.tool_buttons[k] = b
            b.clicked.connect(lambda _c=False, k=k: self.set_tool(k))
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        brow = QHBoxLayout()
        self.brush_label = QLabel(t("se_brush"))
        self.brush = QSlider(Qt.Horizontal)
        self.brush.setRange(3, 150)
        self.brush.setValue(int(cfg.get("brush_px", win.wave.brush_px)))
        self.brush_val = QLabel("")
        self.brush.valueChanged.connect(self._brush_changed)
        brow.addWidget(self.brush_label)
        brow.addWidget(self.brush, 1)
        brow.addWidget(self.brush_val)
        lay.addLayout(brow)
        lay.addWidget(_hint(t("se_hint")))
        self.info = QLabel("")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        form = QFormLayout()
        self.action = QComboBox()
        for k in spectral.ACTIONS:
            self.action.addItem(t("se_a_" + k), k)
        self.action.setCurrentIndex(max(0, self.action.findData(cfg.get("action", "attenuate"))))
        form.addRow(t("se_action"), self.action)
        self.db = QDoubleSpinBox()
        self.db.setRange(0.5, 60.0)
        self.db.setDecimals(1)
        self.db.setSuffix(" dB")
        self.db.setValue(float(cfg.get("db", 12.0)))
        form.addRow(t("se_db"), self.db)
        self.feather_t = QDoubleSpinBox()
        self.feather_t.setRange(0.0, 200.0)
        self.feather_t.setDecimals(0)
        self.feather_t.setSuffix(" " + t("un_ms"))
        self.feather_t.setValue(float(cfg.get("feather_ms", 10.0)))
        form.addRow(t("se_feather_t"), self.feather_t)
        self.feather_f = QDoubleSpinBox()
        self.feather_f.setRange(0.0, 500.0)
        self.feather_f.setDecimals(0)
        self.feather_f.setSuffix(" " + t("un_Hz"))
        self.feather_f.setValue(float(cfg.get("feather_hz", 30.0)))
        form.addRow(t("se_feather_f"), self.feather_f)
        lay.addLayout(form)
        lrow = QHBoxLayout()
        self.listen_buttons = {}
        for k, icon in (("selection", "fa6s.headphones"), ("result", "fa6s.play"), ("original", "fa6s.backward")):
            b = QPushButton(style.icon(icon), t("se_listen_" + k))
            b.setCheckable(True)
            b.toggled.connect(lambda on, k=k: self._listen_toggled(k, on))
            self.listen_buttons[k] = b
            lrow.addWidget(b)
        lay.addLayout(lrow)
        self.status = QLabel("")
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        brow2 = QHBoxLayout()
        self.b_clear = QPushButton(style.icon("fa6s.eraser"), t("se_clear"))
        self.b_clear.clicked.connect(lambda: self.win.wave.clear_spec())
        brow2.addWidget(self.b_clear)
        brow2.addStretch(1)
        self.b_apply = QPushButton(t("fx_apply"))
        self.b_apply.setObjectName("primary")
        self.b_apply.clicked.connect(self.apply)
        brow2.addWidget(self.b_apply)
        b_close = QPushButton(t("close"))
        b_close.clicked.connect(self.close)
        brow2.addWidget(b_close)
        lay.addLayout(brow2)
        self.action.currentIndexChanged.connect(lambda _i: self._sync())
        for w in (self.db, self.feather_t, self.feather_f):
            w.valueChanged.connect(lambda _v: self._params_changed())
        win.wave.specEdited.connect(self._sync)
        self.rendered.connect(self._rendered)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self._brush_changed(self.brush.value())
        self.set_tool(cfg.get("tool", "rect") if cfg.get("tool") in dict(TOOLS) else "rect")

    # ── the tool ──────────────────────────────────────────────
    def set_tool(self, tool: str) -> None:
        w = self.win
        self.tool_buttons[tool].setChecked(True)
        if tool != "time" and w.wave.mode == "wave":
            w.set_view_mode("spec")                       # drawing needs the spectrogram
        w.wave.set_tool(tool)
        self.cfg["tool"] = tool
        on = tool == "brush"
        for x in (self.brush_label, self.brush, self.brush_val):
            x.setEnabled(on)
        self._sync()

    def _brush_changed(self, v: int) -> None:
        self.win.wave.brush_px = int(v)
        self.cfg["brush_px"] = int(v)
        self.brush_val.setText(f"{v} px")

    def params(self, what: str = "result") -> spectral.Params:
        return spectral.Params(action=self.action.currentData() or "attenuate", db=self.db.value(),
                               feather_ms=self.feather_t.value(), feather_hz=self.feather_f.value(), what=what)

    def _params_changed(self) -> None:
        self._sync()
        if self._listen in ("selection", "result"):
            self._start_listen(self._listen)

    # ── what is selected ──────────────────────────────────────
    def _sel(self):
        d = self.win.doc
        return None if d is None or d.spec_sel is None or d.spec_sel.empty() else d.spec_sel

    def _sync(self) -> None:
        w = self.win
        if w.wave.brush_px != self.brush.value():                # Alt + wheel on the wave
            self.brush.blockSignals(True)
            self.brush.setValue(w.wave.brush_px)
            self.brush.blockSignals(False)
            self._brush_changed(w.wave.brush_px)
        act = self.action.currentData()
        self.db.setEnabled(act in ("attenuate", "boost"))
        sel = self._sel()
        d = w.doc
        if sel is None:
            self.info.setText(t("se_none"))
        else:
            a, b = sel.span()
            lo, hi = sel.freq_span()
            labs = ", ".join(d.labels[c] for c in sel.all_channels() if c < d.channels)
            self.info.setText(t("se_info", fmt_time(a, d.rate), fmt_time(min(b, d.frames), d.rate), fmt_hz(lo),
                                fmt_hz(hi), labs))
        for b in (self.b_apply, self.listen_buttons["selection"], self.listen_buttons["result"],
                  self.listen_buttons["original"], self.b_clear):
            b.setEnabled(sel is not None and not w.job)
        if sel is None and self._listen:
            self.stop_listen()

    # ── listening ─────────────────────────────────────────────
    def _listen_toggled(self, what: str, on: bool) -> None:
        if on:
            for k, b in self.listen_buttons.items():
                if k != what and b.isChecked():
                    b.blockSignals(True)
                    b.setChecked(False)
                    b.blockSignals(False)
            self._start_listen(what)
        elif self._listen == what:
            self.stop_listen()

    def _piece_range(self, sel) -> tuple[int, int]:
        d = self.win.doc
        a, b = spectral.region(sel, d.frames, self.params())
        return a, min(b, a + int(PREVIEW_S * d.rate))

    def _start_listen(self, what: str) -> None:
        w = self.win
        sel = self._sel()
        if sel is None:
            return
        w.stop_play()
        self._listen = what
        a, b = self._piece_range(sel)
        if what == "original":
            self._drop_piece()
            w.player.play(w.doc, a, b, loop=True)
            w._update_transport()
            self.timer.start()
            return
        self._run += 1
        run = self._run
        d = w.doc
        p = self.params(what)
        folder = w.session.folder()
        self.status.setText(t("fx_preparing", 0))

        def work():
            try:
                ra, rb, src = spectral.apply(d, sel, p, limit=(a, b))
                from .doc import Document
                piece = Document(rate=d.rate, channels=d.channels, folder=folder, labels=tuple(d.labels),
                                 layout=d.layout, name="spectral")
                piece.set_initial(src)
                self.rendered.emit(piece, "", run)
            except Exception as e:                                       # noqa: BLE001
                self.rendered.emit(None, str(e) or type(e).__name__, run)
        threading.Thread(target=work, daemon=True).start()

    def _rendered(self, piece, err: str, run: int) -> None:
        if run != self._run or not self._listen:
            if piece is not None:
                piece.close()
            return
        if piece is None:
            self.status.setText(t("fx_failed", err))
            return
        self._drop_piece()
        self._piece = piece
        self.status.setText(t("fx_prepared", f"{piece.frames / piece.rate:.1f}"))
        self.win.player.play(piece, 0, piece.frames, loop=True)
        self.win._update_transport()
        self.timer.start()

    def _drop_piece(self) -> None:
        if self._piece is not None:
            if self.win.player.doc is self._piece:
                self.win.stop_play()
            self._piece.close()
            self._piece = None

    def stop_listen(self) -> None:
        self._run += 1
        self._listen = ""
        self.timer.stop()
        if self.win.player.state != "stop":
            self.win.stop_play()
        self._drop_piece()
        for b in self.listen_buttons.values():
            b.blockSignals(True)
            b.setChecked(False)
            b.blockSignals(False)
        self.status.setText("")

    def _tick(self) -> None:
        if self._listen and self.win.player.state == "stop":
            self.stop_listen()

    # ── apply ─────────────────────────────────────────────────
    def apply(self) -> None:
        w = self.win
        sel = self._sel()
        d = w._edit_doc() if sel is not None else None
        if d is None:
            return
        self.stop_listen()
        p = self.params()
        self.cfg.update(action=p.action, db=p.db, feather_ms=p.feather_ms, feather_hz=p.feather_hz)
        from . import config
        config.save(w.cfg)
        label = t("se_undo", t("se_a_" + p.action))
        keep_sel = d.sel
        a0, b0 = spectral.region(sel, d.frames, p)
        w._make_room(d, b0 - a0)

        def work(pr):
            return spectral.apply(d, sel, p, pr)

        def done(res, err):
            if err:
                if err != "cancel":
                    msg = t("se_fill_long", int(spectral.FILL_MAX_S)) if "fill_long" in err else t("op_failed", err)
                    QMessageBox.warning(self, t("se_title"), msg)
                return
            a, b, src = res
            d.replace(a, b, src, label, sel=keep_sel)
            w._edited()
            self._sync()
        w.run_job(t("processing", label), work, done, d)

    # ── closing ───────────────────────────────────────────────
    def closeEvent(self, ev) -> None:
        self.stop_listen()
        self.win.wave.set_tool("time")
        try:
            self.win.wave.specEdited.disconnect(self._sync)
        except (RuntimeError, TypeError):
            pass
        from . import config
        config.save(self.win.cfg)
        if getattr(self.win, "se_panel", None) is self:
            self.win.se_panel = None
        super().closeEvent(ev)
