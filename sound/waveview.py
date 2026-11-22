"""The waveform widgets: WaveView (time ruler on top, dB scale on the left, a lane per channel, the selection, the
edit cursor and the play cursor), Overview (the whole file in one strip, the visible part framed) and Meters (peak
level bars with peak hold and clip lights). The waveform is drawn from the document's peak levels into a cached
pixmap; the cursors and the selection are painted over it, so a moving play cursor costs almost nothing."""
from __future__ import annotations

import math
import time

import numpy as np
from PySide6.QtCore import QLineF, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import style
from .doc import fmt_time
from .i18n import t

RULER_H = 24
GUTTER_W = 46
MIN_SPP = 1 / 32               # at most 32 pixels per sample
DB_MARKS = (0, -3, -6, -12, -18, -24, -36, -48, -60)


def _font(px: int, bold: bool = False) -> QFont:
    f = QFont()
    f.setPixelSize(px)
    f.setBold(bold)
    return f


def nice_step(sec_per_px: float, min_px: float = 92) -> tuple[float, int]:
    """A ruler step (seconds) whose labels are at least min_px apart, and how many minor ticks it splits into."""
    steps = (0.0001, 0.0002, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60,
             120, 300, 600, 900, 1800, 3600, 7200)
    for s in steps:
        if s / sec_per_px >= min_px:
            return s, (5 if str(s)[0] in "15" else 4)
    return steps[-1], 4


def ruler_label(sec: float, step: float) -> str:
    sec = max(0.0, sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if step >= 1:
        return f"{int(h)}:{int(m):02d}:{int(round(s)) % 60:02d}" if h else f"{int(m)}:{int(round(s)) % 60:02d}"
    digits = 4 if step < 0.001 else 3 if step < 0.01 else 2 if step < 0.1 else 1
    txt = f"{s:0{3 + digits}.{digits}f}"
    return (f"{int(h)}:{int(m):02d}:" if h else f"{int(m)}:") + txt


class WaveView(QWidget):
    """The editing view of one document."""
    selectionChanged = Signal()          # selection or edit cursor moved
    viewChanged = Signal()               # scrolled / zoomed
    seekRequested = Signal(int)          # a click while playing: jump the playback there
    contextRequested = Signal(object)    # QPoint (global) — the window builds the menu
    filesDropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc = None
        self.play_pos: int | None = None
        self._marks = None               # (doc, version, frames): the clicks found by Effects → Click removal
        self._cache: QPixmap | None = None
        self._cache_key = None
        self._drag = None                # anchor frame while selecting with the mouse
        self._drag_x = 0
        self._auto = QTimer(self)
        self._auto.setInterval(30)
        self._auto.timeout.connect(self._auto_scroll)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAcceptDrops(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(140)
        self.setAttribute(Qt.WA_OpaquePaintEvent)

    def sizeHint(self) -> QSize:
        return QSize(900, 420)

    # ── geometry ──────────────────────────────────────────────
    def lanes_rect(self) -> QRectF:
        return QRectF(GUTTER_W, RULER_H, max(1, self.width() - GUTTER_W), max(1, self.height() - RULER_H))

    def lane_width(self) -> int:
        return max(1, self.width() - GUTTER_W)

    def spp(self) -> float:
        """Frames per pixel now (the document's zoom; 0 there means «fit the whole file»)."""
        d = self.doc
        if d is None or d.frames <= 0:
            return 1.0
        fit = max(MIN_SPP, d.frames / self.lane_width())
        return fit if d.spp <= 0 else max(MIN_SPP, min(d.spp, fit))

    def fit_spp(self) -> float:
        d = self.doc
        return max(MIN_SPP, (d.frames if d else 0) / self.lane_width())

    def start(self) -> float:
        d = self.doc
        if d is None:
            return 0.0
        return self._clamp_start(d.view_start)

    def _clamp_start(self, s: float) -> float:
        d = self.doc
        span = self.lane_width() * self.spp()
        return max(0.0, min(float(s), max(0.0, d.frames - span)))

    def visible(self) -> tuple[float, float]:
        s = self.start()
        return s, s + self.lane_width() * self.spp()

    def frame_at(self, x: float) -> int:
        d = self.doc
        f = self.start() + (x - GUTTER_W) * self.spp()
        return int(max(0, min(d.frames, round(f))))

    def x_of(self, frame: float) -> float:
        return GUTTER_W + (frame - self.start()) / self.spp()

    # ── changes from outside ──────────────────────────────────
    def set_doc(self, doc) -> None:
        self.doc = doc
        self.play_pos = None
        self._cache_key = None
        self.update()
        self.viewChanged.emit()

    def set_marks(self, doc, frames) -> None:
        """Red marks at these frames (the clicks found) until the document changes."""
        self._marks = (doc, doc.version, list(frames)) if frames else None
        self.update()

    def marks(self) -> list:
        m = self._marks
        if m is None or m[0] is not self.doc or m[1] != self.doc.version:
            return []
        return m[2]

    def refresh(self) -> None:
        """The document changed (an edit / undo): redraw everything."""
        self._cache_key = None
        if self.doc is not None:
            self.doc.view_start = self.start()
        self.update()
        self.viewChanged.emit()

    def set_play_pos(self, pos: int | None, follow: bool = False) -> None:
        old = self.play_pos
        self.play_pos = pos
        if pos is not None and follow and self.doc is not None:
            a, b = self.visible()
            if pos < a or pos > b:
                self.scroll_to(pos - (b - a) * 0.02)
                return
        if old != pos:
            self.update()

    def set_view(self, start: float, spp: float | None = None) -> None:
        d = self.doc
        if d is None:
            return
        if spp is not None:
            fit = self.fit_spp()
            d.spp = 0.0 if spp >= fit * 0.999 else max(MIN_SPP, spp)
        d.view_start = self._clamp_start(start)
        self.update()
        self.viewChanged.emit()

    def scroll_to(self, start: float) -> None:
        self.set_view(start)

    def zoom(self, factor: float, anchor_x: float | None = None) -> None:
        """factor < 1 zooms in. The frame under anchor_x (default: the middle, or the cursor when visible) stays."""
        d = self.doc
        if d is None or d.frames <= 0:
            return
        if anchor_x is None:
            a, b = self.visible()
            anchor_x = self.x_of(d.cursor) if a <= d.cursor <= b else GUTTER_W + self.lane_width() / 2
        f = self.start() + (anchor_x - GUTTER_W) * self.spp()
        new = max(MIN_SPP, min(self.fit_spp(), self.spp() * factor))
        self.set_view(f - (anchor_x - GUTTER_W) * new, new)

    def zoom_all(self) -> None:
        if self.doc is not None:
            self.doc.spp = 0.0
            self.set_view(0, self.fit_spp())

    def zoom_range(self, a: int, b: int) -> None:
        if self.doc is None or b <= a:
            return
        spp = (b - a) / (self.lane_width() * 0.96)
        self.set_view(a - (b - a) * 0.02, spp)

    def zoom_samples(self) -> None:
        d = self.doc
        if d is not None:
            self.set_view(d.cursor - self.lane_width() / 2, 1.0)

    def ensure_visible(self, frame: int) -> None:
        a, b = self.visible()
        if frame < a or frame > b:
            self.scroll_to(frame - (b - a) / 2)

    # ── painting ──────────────────────────────────────────────
    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), style.qcolor(style.W_BG))
        d = self.doc
        if d is None:
            self._paint_empty(p)
            return
        lanes = self.lanes_rect()
        key = (d.version, round(self.start(), 3), round(self.spp(), 6), self.width(), self.height(), style.THEME,
               self.devicePixelRatioF())
        if key != self._cache_key or self._cache is None:
            self._cache = self._render(lanes)
            self._cache_key = key
        p.drawPixmap(int(lanes.left()), int(lanes.top()), self._cache)
        self._paint_ruler(p)
        self._paint_gutter(p)
        self._paint_overlay(p)

    def _paint_empty(self, p: QPainter) -> None:
        r = self.rect()
        ic = style.icon("fa6s.wave-square", style.MUTED)
        pm = ic.pixmap(64, 64)
        p.drawPixmap(int(r.center().x() - 32), int(r.center().y() - 70), pm)
        p.setPen(style.qcolor(style.TEXT))
        p.setFont(_font(17, True))
        p.drawText(QRectF(0, r.center().y() + 4, r.width(), 26), Qt.AlignCenter, t("empty_title"))
        p.setPen(style.qcolor(style.MUTED))
        p.setFont(_font(12))
        p.drawText(QRectF(40, r.center().y() + 34, r.width() - 80, 40), Qt.AlignHCenter | Qt.TextWordWrap,
                   t("empty_hint"))

    def lane_geom(self, h: float, n: int) -> list[tuple[float, float]]:
        """(top, height) of each channel lane inside a lanes area of height h."""
        gap = 1.0
        lh = (h - gap * (n - 1)) / max(1, n)
        return [(i * (lh + gap), lh) for i in range(n)]

    def _render(self, lanes: QRectF) -> QPixmap:
        d = self.doc
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(lanes.width() * dpr), int(lanes.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(style.qcolor(style.W_BG))
        p = QPainter(pm)
        w, h = lanes.width(), lanes.height()
        n = d.channels
        geo = self.lane_geom(h, n)
        spp = self.spp()
        start = self.start()
        wave = style.qcolor(style.W_WAVE)
        grid = style.qcolor(style.W_GRID)
        center = style.qcolor(style.W_CENTER)
        end_x = min(w, (d.frames - start) / spp)
        for top, lh in geo:
            p.fillRect(QRectF(0, top, end_x, lh), style.qcolor(style.W_LANE))
            mid = top + lh / 2
            p.setPen(QPen(grid, 1))
            for db in (-6, -12):
                if lh >= (160 if db == -12 else 70):
                    a = 10 ** (db / 20) * lh / 2
                    p.drawLine(QLineF(0, mid - a, end_x, mid - a))
                    p.drawLine(QLineF(0, mid + a, end_x, mid + a))
            p.setPen(QPen(center, 1))
            p.drawLine(QLineF(0, mid, end_x, mid))
        if spp >= 1:
            ncols = int(math.ceil(w))
            mm = d.minmax(start, spp, ncols)
            p.setPen(QPen(wave, 1))
            for c, (top, lh) in enumerate(geo):
                mid, half = top + lh / 2, lh / 2 - 1
                lo = np.clip(mm[:, c, 0], -1.0, 1.0)
                hi = np.clip(mm[:, c, 1], -1.0, 1.0)
                ok = np.flatnonzero(~np.isnan(lo))
                if spp < 64 and len(ok) > 1:       # few samples a column: join each column to the one before
                    lo2, hi2 = lo.copy(), hi.copy()
                    prev_lo, prev_hi = np.roll(lo, 1), np.roll(hi, 1)
                    good = ~np.isnan(prev_lo)
                    good[0] = False
                    lo2[good] = np.minimum(lo[good], prev_hi[good])
                    hi2[good] = np.maximum(hi[good], prev_lo[good])
                    lo, hi = lo2, hi2
                y1 = mid - hi * half
                y2 = mid - lo * half
                lines = [QLineF(x + 0.5, y1[x], x + 0.5, max(y2[x], y1[x] + 1)) for x in ok]
                p.drawLines(lines)
                self._paint_clips(p, mm[:, c], ok, top, lh)
        else:
            a = int(math.floor(start))
            k = int(math.ceil(w * spp)) + 2
            x = d.read(a, k)
            n_ok = max(0, min(k, d.frames - a))
            xs = (np.arange(n_ok) + a - start) / spp
            for c, (top, lh) in enumerate(geo):
                mid, half = top + lh / 2, lh / 2 - 1
                ys = mid - np.clip(x[:n_ok, c], -1, 1) * half
                poly = QPolygonF([QPointF(float(xx), float(yy)) for xx, yy in zip(xs, ys)])
                p.setPen(QPen(wave, 1.2))
                p.drawPolyline(poly)
                if 1 / spp >= 6:
                    p.setBrush(wave)
                    r = 2.2 if 1 / spp >= 12 else 1.6
                    for xx, yy in zip(xs, ys):
                        p.drawEllipse(QPointF(float(xx), float(yy)), r, r)
                        if 1 / spp >= 12:
                            p.drawLine(QLineF(float(xx), mid, float(xx), float(yy)))
                    p.setBrush(Qt.NoBrush)
        p.end()
        return pm

    def _paint_clips(self, p: QPainter, mm: np.ndarray, ok, top: float, lh: float) -> None:
        """Red marks at the top / bottom edge where the sound reaches full scale."""
        hot = ok[(mm[ok, 1] >= 0.9999) | (mm[ok, 0] <= -0.9999)]
        if len(hot):
            p.save()
            p.setPen(QPen(style.qcolor(style.W_CLIP), 1))
            p.drawLines([QLineF(x + 0.5, top, x + 0.5, top + 3) for x in hot] +
                        [QLineF(x + 0.5, top + lh - 3, x + 0.5, top + lh) for x in hot])
            p.restore()

    def _paint_ruler(self, p: QPainter) -> None:
        d = self.doc
        r = QRectF(0, 0, self.width(), RULER_H)
        p.fillRect(r, style.qcolor(style.W_RULER))
        p.setPen(QPen(style.qcolor(style.BORDER), 1))
        p.drawLine(QLineF(0, RULER_H - 0.5, self.width(), RULER_H - 0.5))
        spp = self.spp()
        start = self.start()
        sec_px = spp / d.rate
        step, minor = nice_step(sec_px)
        a_sec = start / d.rate
        b_sec = (start + self.lane_width() * spp) / d.rate
        p.setFont(_font(11))
        text = style.qcolor(style.W_RULER_TEXT)
        sub = step / minor
        k = math.floor(a_sec / sub)
        while k * sub <= b_sec + sub:
            sec = k * sub
            x = GUTTER_W + (sec * d.rate - start) / spp
            if x >= GUTTER_W:
                major = abs(round(sec / step) * step - sec) < sub * 0.01
                p.setPen(QPen(text, 1))
                p.drawLine(QLineF(x, RULER_H - (9 if major else 4), x, RULER_H - 1))
                if major:
                    p.drawText(QRectF(x + 3, 2, 120, RULER_H - 9), Qt.AlignLeft | Qt.AlignVCenter,
                               ruler_label(sec, step))
            k += 1
        p.fillRect(QRectF(0, 0, GUTTER_W, RULER_H), style.qcolor(style.W_RULER))
        p.setPen(QPen(style.qcolor(style.BORDER), 1))
        p.drawLine(QLineF(GUTTER_W - 0.5, 0, GUTTER_W - 0.5, self.height()))

    def _paint_gutter(self, p: QPainter) -> None:
        d = self.doc
        lanes = self.lanes_rect()
        p.fillRect(QRectF(0, RULER_H, GUTTER_W - 1, lanes.height()), style.qcolor(style.W_RULER))
        text = style.qcolor(style.W_RULER_TEXT)
        p.setFont(_font(10))
        for c, (top, lh) in enumerate(self.lane_geom(lanes.height(), d.channels)):
            top += RULER_H
            mid = top + lh / 2
            marks = [(0, 1.0), ("-∞", 0.0)]
            if lh >= 70:
                marks.append((-6, 0.5))
            if lh >= 160:
                marks.append((-12, 0.25))
            p.setPen(QPen(text, 1))
            for db, a in marks:
                for sign in ((1, -1) if a else (1,)):
                    y = mid - sign * a * (lh / 2 - 1)
                    p.drawLine(QLineF(GUTTER_W - 5, y, GUTTER_W - 1, y))
                    label = f"{db}" if isinstance(db, str) else ("0" if db == 0 else str(db))
                    ty = max(top + 1, min(top + lh - 13, y - 6))
                    p.drawText(QRectF(2, ty, GUTTER_W - 9, 12), Qt.AlignRight | Qt.AlignVCenter, label)
            # the channel's name in a small box at the top-left of its lane
            name = d.labels[c] if c < len(d.labels) else str(c + 1)
            p.setFont(_font(10, True))
            box = QRectF(GUTTER_W + 4, top + 3, 8 + 7 * len(name), 15)
            p.fillRect(box, style.qcolor(style.W_RULER))
            p.setPen(QPen(style.qcolor(style.TEXT), 1))
            p.drawText(box, Qt.AlignCenter, name)
            p.setFont(_font(10))
            if c:
                p.setPen(QPen(style.qcolor(style.BORDER), 1))
                p.drawLine(QLineF(0, top - 0.5, self.width(), top - 0.5))

    def _paint_overlay(self, p: QPainter) -> None:
        d = self.doc
        lanes = self.lanes_rect()
        p.save()
        p.setClipRect(QRectF(GUTTER_W, 0, lanes.width(), self.height()))
        if d.sel:
            x1, x2 = self.x_of(d.sel[0]), self.x_of(d.sel[1])
            x2 = max(x2, x1 + 1)
            # «screen» lightens the background and the wave alike on the dark theme, «multiply» tints them on the
            # light one — the selected wave stays as readable as the rest
            p.setCompositionMode(QPainter.CompositionMode_Screen if style.IS_DARK else
                                 QPainter.CompositionMode_Multiply)
            p.fillRect(QRectF(x1, RULER_H, x2 - x1, lanes.height()), style.qcolor(style.W_SEL))
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.fillRect(QRectF(x1, RULER_H - 5, x2 - x1, 5), style.qcolor(style.ACCENT))
        xc = self.x_of(d.cursor)
        pen = QPen(style.qcolor(style.W_CURSOR), 1, Qt.DashLine)
        p.setPen(pen)
        p.drawLine(QLineF(xc + 0.5, RULER_H, xc + 0.5, self.height()))
        p.setPen(Qt.NoPen)
        p.setBrush(style.qcolor(style.W_CURSOR))
        p.drawPolygon(QPolygonF([QPointF(xc - 5, RULER_H - 7), QPointF(xc + 5, RULER_H - 7),
                                 QPointF(xc + 0.5, RULER_H - 1)]))
        marks = self.marks()
        if marks:
            a, b = self.visible()
            col = style.qcolor(style.W_CLIP)
            p.setPen(QPen(col, 1))
            p.setBrush(col)
            for f in marks:
                if a <= f <= b:
                    xm = self.x_of(f)
                    p.drawLine(QLineF(xm, RULER_H, xm, self.height()))
                    p.drawPolygon(QPolygonF([QPointF(xm - 4, RULER_H), QPointF(xm + 4, RULER_H),
                                             QPointF(xm, RULER_H + 7)]))
            p.setBrush(Qt.NoBrush)
        if self.play_pos is not None:
            xp = self.x_of(self.play_pos)
            p.setPen(QPen(style.qcolor(style.W_PLAY), 2))
            p.drawLine(QLineF(xp, 0, xp, self.height()))
        p.restore()

    # ── mouse and keys ────────────────────────────────────────
    def mousePressEvent(self, ev) -> None:
        d = self.doc
        if d is None:
            return
        x, y = ev.position().x(), ev.position().y()
        if ev.button() == Qt.RightButton:
            self.contextRequested.emit(ev.globalPosition().toPoint())
            return
        if ev.button() != Qt.LeftButton or x < GUTTER_W:
            return
        f = self.frame_at(x)
        if y < RULER_H:
            d.cursor = f
            self.seekRequested.emit(f)
            self.selectionChanged.emit()
            self.update()
            return
        if ev.modifiers() & Qt.ShiftModifier:
            if d.sel:
                a, b = d.sel
                anchor = b if abs(f - a) < abs(f - b) else a
            else:
                anchor = d.cursor
            self._drag = anchor
            self._set_sel(anchor, f)
        else:
            self._drag = f
            d.sel = None
            d.cursor = f
            self.seekRequested.emit(f)
        self._drag_x = x
        self.selectionChanged.emit()
        self.update()

    def _set_sel(self, anchor: int, f: int) -> None:
        d = self.doc
        a, b = min(anchor, f), max(anchor, f)
        d.sel = (a, b) if b > a else None
        d.cursor = f if d.sel else anchor

    def mouseMoveEvent(self, ev) -> None:
        if self._drag is None or self.doc is None:
            return
        x = ev.position().x()
        self._drag_x = x
        if abs(x - self.x_of(self._drag)) >= 2 or self.doc.sel:
            self._set_sel(self._drag, self.frame_at(max(GUTTER_W, min(self.width(), x))))
            self.selectionChanged.emit()
            self.update()
        if x < GUTTER_W or x > self.width():
            if not self._auto.isActive():
                self._auto.start()
        else:
            self._auto.stop()

    def _auto_scroll(self) -> None:
        if self._drag is None:
            self._auto.stop()
            return
        x = self._drag_x
        over = x - GUTTER_W if x < GUTTER_W else x - self.width()
        a, b = self.visible()
        self.scroll_to(a + over * self.spp() * 1.5)
        edge = GUTTER_W if over < 0 else self.width()
        self._set_sel(self._drag, self.frame_at(edge))
        self.selectionChanged.emit()

    def mouseReleaseEvent(self, ev) -> None:
        self._drag = None
        self._auto.stop()

    def mouseDoubleClickEvent(self, ev) -> None:
        d = self.doc
        if d is not None and ev.button() == Qt.LeftButton and ev.position().y() >= RULER_H and d.frames:
            d.sel = (0, d.frames)
            self.selectionChanged.emit()
            self.update()

    def wheelEvent(self, ev) -> None:
        d = self.doc
        if d is None:
            return
        dy = ev.angleDelta().y()
        dx = ev.angleDelta().x()
        if ev.modifiers() & Qt.ControlModifier:
            if dy:
                self.zoom(0.8 ** (dy / 120), ev.position().x())
        else:
            steps = (dx or dy) / 120
            self.scroll_to(self.start() - steps * self.lane_width() * self.spp() * 0.15)
        ev.accept()

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self.viewChanged.emit()

    # ── drag & drop of files ──────────────────────────────────
    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.filesDropped.emit(paths)
            ev.acceptProposedAction()


class Overview(QWidget):
    """The whole file in one strip: the view's part framed; click or drag to go there."""
    scrollRequested = Signal(float)      # the frame to put in the middle of the view

    def __init__(self, view: WaveView, parent=None):
        super().__init__(parent)
        self.view = view
        self._cache = None
        self._key = None
        self.setFixedHeight(42)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        r = self.rect()
        p.fillRect(r, style.qcolor(style.W_RULER))
        d = self.view.doc
        if d is None or d.frames <= 0:
            return
        w = max(1, self.width() - GUTTER_W)
        key = (d.version, w, self.height(), style.THEME)
        if key != self._key:
            self._cache = d.minmax(0, max(1.0, d.frames / w), w)
            self._key = key
        mm = self._cache
        lo = np.nan_to_num(np.clip(mm[:, :, 0].min(axis=1), -1, 1))
        hi = np.nan_to_num(np.clip(mm[:, :, 1].max(axis=1), -1, 1))
        h = self.height() - 4
        mid = 2 + h / 2
        p.setPen(QPen(style.qcolor(style.W_WAVE), 1))
        p.drawLines([QLineF(GUTTER_W + x + 0.5, mid - hi[x] * h / 2, GUTTER_W + x + 0.5,
                            max(mid - lo[x] * h / 2, mid - hi[x] * h / 2 + 1)) for x in range(len(lo))])
        k = w / d.frames
        if d.sel:
            p.setCompositionMode(QPainter.CompositionMode_Screen if style.IS_DARK else
                                 QPainter.CompositionMode_Multiply)
            p.fillRect(QRectF(GUTTER_W + d.sel[0] * k, 0, max(1.0, (d.sel[1] - d.sel[0]) * k), self.height()),
                       style.qcolor(style.W_SEL))
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        a, b = self.view.visible()
        vr = QRectF(GUTTER_W + a * k, 0.5, max(3.0, (min(b, d.frames) - a) * k), self.height() - 1)
        p.fillRect(vr, style.qcolor(style.W_OVER_VIEW))
        p.setPen(QPen(style.qcolor(style.ACCENT), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(vr)
        p.setPen(QPen(style.qcolor(style.W_CURSOR), 1))
        xc = GUTTER_W + d.cursor * k
        p.drawLine(QLineF(xc, 0, xc, self.height()))
        if self.view.play_pos is not None:
            p.setPen(QPen(style.qcolor(style.W_PLAY), 2))
            xp = GUTTER_W + self.view.play_pos * k
            p.drawLine(QLineF(xp, 0, xp, self.height()))
        p.setPen(QPen(style.qcolor(style.BORDER), 1))
        p.drawLine(QLineF(0, self.height() - 0.5, self.width(), self.height() - 0.5))
        p.setFont(_font(10, True))
        p.setPen(style.qcolor(style.W_RULER_TEXT))
        p.drawText(QRectF(0, 0, GUTTER_W - 4, self.height()), Qt.AlignCenter, fmt_time(d.frames, d.rate, False))

    def _go(self, x: float) -> None:
        d = self.view.doc
        if d is None or d.frames <= 0:
            return
        w = max(1, self.width() - GUTTER_W)
        self.scrollRequested.emit(max(0.0, min(1.0, (x - GUTTER_W) / w)) * d.frames)

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            self._go(ev.position().x())

    def mouseMoveEvent(self, ev) -> None:
        if ev.buttons() & Qt.LeftButton:
            self._go(ev.position().x())


class Meters(QWidget):
    """A bar per channel, −60…0 dBFS: the level falls back at 24 dB/s, the peak holds 1.5 s, a red light stays on
    after a sample at full scale until clicked."""
    FLOOR = -60.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.labels: list[str] = ["L", "R"]
        self.level = np.full(2, self.FLOOR)
        self.peak = np.full(2, self.FLOOR)
        self.peak_t = np.zeros(2)
        self.max_db = np.full(2, self.FLOOR)
        self.clip = np.zeros(2, bool)
        self._last = time.monotonic()
        self.setMinimumWidth(96)
        self.setToolTip(t("meter_reset"))
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

    def sizeHint(self) -> QSize:
        return QSize(40 + 22 * len(self.labels), 300)

    def set_channels(self, labels) -> None:
        labels = list(labels) or ["L", "R"]
        if labels != self.labels:
            self.labels = labels
            n = len(labels)
            self.level = np.full(n, self.FLOOR)
            self.peak = np.full(n, self.FLOOR)
            self.peak_t = np.zeros(n)
            self.max_db = np.full(n, self.FLOOR)
            self.clip = np.zeros(n, bool)
            self.setMinimumWidth(max(96, 40 + 22 * n))
            self.updateGeometry()
        self.update()

    def reset(self) -> None:
        self.peak[:] = self.FLOOR
        self.max_db[:] = self.FLOOR
        self.clip[:] = False
        self.update()

    def feed(self, peaks) -> None:
        """New peak values (linear, per channel) or None (silence / stopped)."""
        now = time.monotonic()
        dt = min(0.2, now - self._last)
        self._last = now
        n = len(self.labels)
        if peaks is None:
            db = np.full(n, self.FLOOR)
        else:
            v = np.asarray(peaks, np.float64)[:n]
            if len(v) < n:
                v = np.concatenate((v, np.zeros(n - len(v))))
            db = np.where(v > 1e-6, 20 * np.log10(np.maximum(v, 1e-6)), self.FLOOR)
            self.clip |= v >= 0.9999
            self.max_db = np.maximum(self.max_db, db)
        self.level = np.maximum(db, self.level - 24 * dt)
        hold = db >= self.peak
        self.peak = np.where(hold, db, self.peak)
        self.peak_t = np.where(hold, now, self.peak_t)
        old = now - self.peak_t > 1.5
        self.peak = np.where(old, np.maximum(self.level, self.peak - 30 * dt), self.peak)
        self.update()

    def mousePressEvent(self, ev) -> None:
        self.reset()

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), style.qcolor(style.PANEL))
        n = len(self.labels)
        top, bottom = 34, self.height() - 20
        scale_w = 28
        bw = max(8.0, min(22.0, (self.width() - scale_w - 8 - 3 * (n - 1)) / n))
        x0 = scale_w + 4
        h = bottom - top

        def y_of(db):
            return top + h * (min(0.0, db) / self.FLOOR)
        p.setFont(_font(10))
        p.setPen(style.qcolor(style.MUTED))
        for db in DB_MARKS:
            y = y_of(db)
            p.drawText(QRectF(0, y - 6, scale_w - 3, 12), Qt.AlignRight | Qt.AlignVCenter, str(db))
            p.drawLine(QLineF(scale_w, y, scale_w + 2, y))
        for i, lab in enumerate(self.labels):
            x = x0 + i * (bw + 3)
            p.fillRect(QRectF(x, top, bw, h), style.qcolor(style.W_METER))
            lv = self.level[i]
            if lv > self.FLOOR:
                for a, b, col in ((self.FLOOR, -18, "#2fbf71"), (-18, -6, "#e8c547"), (-6, 0, "#ef5350")):
                    if lv > a:
                        y1, y2 = y_of(min(lv, b)), y_of(a)
                        p.fillRect(QRectF(x, y1, bw, y2 - y1), QColor(col))
            pk = self.peak[i]
            if pk > self.FLOOR:
                p.fillRect(QRectF(x, y_of(pk) - 1, bw, 2), QColor("#ffffff" if style.IS_DARK else "#202020"))
            p.fillRect(QRectF(x, top - 8, bw, 5), QColor(style.W_CLIP) if self.clip[i] else
                       style.qcolor(style.W_GRID))
            p.setPen(style.qcolor(style.TEXT))
            p.setFont(_font(9, True))
            p.drawText(QRectF(x - 4, bottom + 3, bw + 8, 14), Qt.AlignCenter, lab)
            m = self.max_db[i]
            p.setFont(_font(9))
            p.setPen(style.qcolor(style.W_CLIP) if self.clip[i] else style.qcolor(style.MUTED))
            p.drawText(QRectF(x - 6, top - 26, bw + 12, 14), Qt.AlignCenter,
                       "−∞" if m <= self.FLOOR else f"{m:.1f}".replace("-", "−"))
