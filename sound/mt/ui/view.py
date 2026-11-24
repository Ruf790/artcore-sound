"""The multitrack tab's body (3.0): the track headers and the timeline with their scroll bars over the mixer (a
splitter between them; the mixer hides and shows with F9). The master's meters and the list of markers are the
window's own side panel, as for a document."""
from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QScrollBar, QSplitter, QVBoxLayout, QWidget

from ...i18n import t
from .headers import HeaderColumn
from .mixer import MixerPanel
from .timeline import Timeline


class MultitrackView(QWidget):
    edited = Signal()                   # an undo step was made here
    cursorMoved = Signal()
    action = Signal(int, str)           # a header's / strip's button the window handles (fx, menu, arm …)
    addRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.p = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.split = QSplitter(Qt.Vertical)
        self.split.setChildrenCollapsible(True)
        top = QWidget()
        g = QHBoxLayout(top)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(0)
        self.timeline = Timeline()
        self.headers = HeaderColumn(self.timeline)
        g.addWidget(self.headers)
        mid = QVBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(0)
        mid.addWidget(self.timeline, 1)
        self.hbar = QScrollBar(Qt.Horizontal)
        mid.addWidget(self.hbar)
        g.addLayout(mid, 1)
        self.vbar = QScrollBar(Qt.Vertical)
        g.addWidget(self.vbar)
        self.split.addWidget(top)
        self.mixer = MixerPanel()
        self.split.addWidget(self.mixer)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        root.addWidget(self.split, 1)
        self._lock = False
        self.hbar.valueChanged.connect(self._on_hbar)
        self.vbar.valueChanged.connect(lambda v: self.timeline.set_yoff(v) if not self._lock else None)
        self.timeline.viewChanged.connect(self._sync_bars)
        self.timeline.scrolled.connect(self._on_scrolled)
        self.timeline.edited.connect(self._edited)
        self.timeline.cursorMoved.connect(self._cursor)
        self.headers.changed.connect(self.apply_field)
        self.mixer.changed.connect(self.apply_field)
        self.headers.action.connect(self._action)
        self.mixer.action.connect(self._action)
        self.headers.addRequested.connect(self.addRequested)

    def set_project(self, p) -> None:
        self.p = p
        self.timeline.set_project(p)
        self.headers.set_project(p)
        self.mixer.set_project(p)
        if p is not None:
            self.mixer.setVisible(bool(p.ui.get("mixer", True)))
        self.sync()

    def sync(self) -> None:
        """After any change of the project: everything shows its state."""
        if self.p is None:
            return
        self.headers.sync()
        self.mixer.sync()
        self.timeline.refresh()
        self._sync_bars()

    def _edited(self) -> None:
        self.headers.sync()
        self.mixer.sync()
        self._sync_bars()
        self.edited.emit()

    def _cursor(self) -> None:
        self.headers.sync()
        self.cursorMoved.emit()

    def _on_scrolled(self) -> None:
        self.headers.relayout()
        self._sync_bars()

    def _action(self, tid: int, what: str) -> None:
        p = self.p
        if p is None:
            return
        if what == "select":
            p.ui["current"] = tid
            self.headers.sync()
            self.timeline.refresh()
            self.cursorMoved.emit()
            return
        if what in ("env_on", "env_off"):
            env = set(p.ui.get("env") or [])
            (env.add if what == "env_on" else env.discard)(tid)
            p.ui["env"] = sorted(env)
            self.timeline.refresh()
            return
        self.action.emit(tid, what)

    def apply_field(self, tid: int, field: str, v, merge: str) -> None:
        """A header's or a strip's control → the project (a fader drag is one undo step: the merge key)."""
        p = self.p
        if p is None:
            return
        tr = p.track(tid)
        if tr is None:
            return
        mk = f"{merge}:{tid}" if merge else ""
        if field in ("gain_db", "pan", "mute", "solo", "phase", "name", "output"):
            label = {"gain_db": t("mt_u_volume"), "pan": t("mt_u_pan"), "mute": t("mt_mute"), "solo": t("mt_solo"),
                     "phase": t("mt_phase"), "name": t("mt_u_rename"), "output": t("mt_output")}[field]
            p.set_track(tid, label, merge=mk, **{field: v})
        elif field == "send":
            bus, db = v
            p.set_send(tid, bus, db=db, label=t("mt_u_send"), merge=mk)
        elif field == "send_pre":
            bus, pre = v
            p.set_send(tid, bus, pre=pre, label=t("mt_u_send"))
        elif field == "send_remove":
            p.set_send(tid, v, remove=True, label=t("mt_u_send"))
        elif field == "fx_bypass":
            sid, off = v
            p.set_fx(tid, tuple(replace(s, bypass=off) if s.id == sid else s for s in tr.fx), t("mt_u_fx"))
        self.timeline.refresh()
        self.headers.sync()
        self.mixer.sync()
        self.edited.emit()

    # ── scroll bars ───────────────────────────────────────────
    def _sync_bars(self) -> None:
        p = self.p
        if p is None:
            return
        self._lock = True
        try:
            tl = self.timeline
            total = max(p.frames, p.rate * 60) + tl.width() * tl.spp() * 0.5
            span = tl.width() * tl.spp()
            unit = max(1.0, total / 100000)
            self._hunit = unit
            self.hbar.setRange(0, int(max(0, total - span) / unit))
            self.hbar.setPageStep(int(span / unit))
            self.hbar.setSingleStep(max(1, int(span / unit / 20)))
            self.hbar.setValue(int(tl.start() / unit))
            ch = tl.content_h() + 40
            vh = tl.view_h()
            self.vbar.setRange(0, max(0, ch - vh))
            self.vbar.setPageStep(max(1, vh))
            self.vbar.setSingleStep(30)
            self.vbar.setValue(tl.yoff)
            self.vbar.setVisible(ch > vh)
        finally:
            self._lock = False
        self.headers.relayout()

    def _on_hbar(self, v: int) -> None:
        if not self._lock and self.p is not None:
            self.timeline.set_view(v * getattr(self, "_hunit", 1.0))

    def show_mixer(self, on: bool) -> None:
        if self.p is not None:
            self.p.ui["mixer"] = bool(on)
        self.mixer.setVisible(bool(on))

    def feed(self, meters: dict) -> None:
        self.headers.feed(meters)
        if self.mixer.isVisible():
            self.mixer.feed(meters)

