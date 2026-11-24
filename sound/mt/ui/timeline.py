"""The timeline of a project (3.0): the time ruler with markers and regions on top, a lane per track below with its
clips (waveform from the peak levels, name, fades, the take), the volume and pan envelopes when shown, the time
selection, the edit cursor and the play cursor.

Mouse: a click in a lane puts the cursor there and makes the track current, a drag selects time (over the tracks
dragged across); a click on a clip selects it (Ctrl adds), a drag moves it (to other tracks too; Ctrl copies),
its edges trim it, the squares at its top corners set the fades, the dot at its top middle its volume; with the
envelopes shown a click on a line adds a point, a point is dragged, a double click removes it. Everything snaps to
clip edges, markers, the cursor and the ruler's grid (Shift — no snapping). Wheel: tracks up / down; Shift: time;
Ctrl: zoom. A drag changes the project at once (playing goes on with it) and is one undo step."""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from ... import style
from ...doc import Document
from ...i18n import t
from ...proc import curve_gain
from ...waveview import MARK_H, RULER_H, TIME_H, nice_step, ruler_label
from ..model import ENV_MAX_DB, ENV_MIN_DB, env_value

LANE_H = 96                     # an audio track's lane by default
BUS_H = 60
MIN_LANE = 44
MAX_LANE = 400
CLIP_HEAD = 15                  # the clip's name bar
EDGE_PX = 6                     # a clip's edge this close trims it
HANDLE = 8                      # the fade / gain handles
SNAP_PX = 8
MIN_SPP = 1 / 16

COLORS = ("#3ea6ff", "#37d67a", "#ffb02e", "#ff6b6b", "#b07cff", "#2dd4bf", "#f472b6", "#a3e635", "#fb923c",
          "#60a5fa", "#facc15", "#94a3b8")


def track_color(i: int) -> QColor:
    return QColor(COLORS[int(i) % len(COLORS)])


def _font(px: int, bold: bool = False) -> QFont:
    f = QFont()
    f.setPixelSize(px)
    f.setBold(bold)
    return f


def lane_height(t) -> int:
    if t.height:
        return max(MIN_LANE, min(MAX_LANE, t.height))
    return BUS_H if t.kind == "bus" else LANE_H


def lanes(project) -> list:
    """(track, top, height) from the top of the lanes (content coordinates)."""
    out, y = [], 0
    for t in project.tracks:
        h = lane_height(t)
        out.append((t, y, h))
        y += h
    return out


class _Peaks:
    """The waveform columns of a medium (a one-piece Document per medium: its minmax reads the peak levels)."""

    def __init__(self):
        self.docs: dict = {}

    def columns(self, m, start: float, spp: float, ncols: int) -> np.ndarray | None:
        """(ncols, 2) min / max over the channels from medium frame `start`, NaN past its end."""
        if m is None or m.src is None or ncols <= 0:
            return None
        d = self.docs.get(m.id)
        if d is None or not d.pieces or d.pieces[0][0] is not m.src:
            d = Document(rate=m.rate or 48000, channels=m.src.channels, folder="", labels=tuple(m.labels) or
                         tuple(str(i) for i in range(m.src.channels)))
            d._set([(m.src, 0, m.src.frames)])
            self.docs[m.id] = d
        if spp < 1:                                  # zoomed past single frames: a column per frame, repeated
            k = int(math.ceil(ncols * spp)) + 1
            mm = d.minmax(max(0.0, start), 1.0, k)
            idx = np.minimum(k - 1, (np.arange(ncols) * spp + (start - math.floor(start))).astype(np.int64))
            mm = mm[idx]
        else:
            mm = d.minmax(max(0.0, start), spp, ncols)
        lo = np.nanmin(mm[..., 0], axis=1) if mm.shape[1] > 1 else mm[:, 0, 0]
        hi = np.nanmax(mm[..., 1], axis=1) if mm.shape[1] > 1 else mm[:, 0, 1]
        return np.stack((lo, hi), axis=1)


class Timeline(QWidget):
    edited = Signal()                     # the project changed (an undo step)
    cursorMoved = Signal()                # the cursor / the selection / the current track changed
    seekRequested = Signal(int)
    viewChanged = Signal()
    contextRequested = Signal(object, object, int, object)   # global pos, track or None, frame, clip or None
    filesDropped = Signal(list, object, int)                  # paths, track id or None, frame
    openClip = Signal(int)
    scrolled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.p = None                     # the project
        self.yoff = 0                     # the vertical scroll (pixels of lanes above the view)
        self.play_pos = None
        self.rec_preview: dict = {}       # track id → (start, frames) of a recording under way
        self.media_frames = lambda mid: None
        self._peaks = _Peaks()
        self._cache = None
        self._cache_key = None
        self._drag = None
        self._snap_x = None
        self._hover = None
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(160)
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(40)
        self._scroll_timer.timeout.connect(self._auto_scroll)

    def sizeHint(self) -> QSize:
        return QSize(900, 420)

    # ── geometry ──────────────────────────────────────────────
    def set_project(self, p) -> None:
        self.p = p
        self.yoff = int(p.ui.get("yoff", 0)) if p is not None else 0
        self._peaks = _Peaks()
        self._drag = None
        self.refresh()

    def refresh(self) -> None:
        self._cache_key = None
        self.update()

    def content_h(self) -> int:
        return sum(h for _t, _y, h in lanes(self.p)) if self.p is not None else 0

    def view_h(self) -> int:
        return max(0, self.height() - RULER_H)

    def set_yoff(self, y: int) -> None:
        y = int(max(0, min(max(0, self.content_h() - self.view_h() + 40), y)))
        if y != self.yoff:
            self.yoff = y
            if self.p is not None:
                self.p.ui["yoff"] = y
            self.refresh()
            self.scrolled.emit()

    def fit_spp(self) -> float:
        p = self.p
        n = max(p.frames if p else 0, int((p.rate if p else 48000) * 60))
        return max(MIN_SPP, n * 1.05 / max(1, self.width()))

    def spp(self) -> float:
        p = self.p
        if p is None:
            return 1.0
        if p.spp <= 0:
            p.spp = self.fit_spp()
        return p.spp

    def start(self) -> float:
        return self.p.view_start if self.p is not None else 0.0

    def visible(self) -> tuple[float, float]:
        s = self.start()
        return s, s + self.width() * self.spp()

    def x_of(self, frame: float) -> float:
        return (frame - self.start()) / self.spp()

    def frame_at(self, x: float) -> int:
        return int(max(0, round(self.start() + x * self.spp())))

    def y_top(self, content_y: float) -> float:
        return RULER_H + content_y - self.yoff

    def lane_at(self, y: float):
        """(track, top, height) of the lane under widget y (top in widget coordinates), or None."""
        if self.p is None or y < RULER_H:
            return None
        for t, top, h in lanes(self.p):
            yt = self.y_top(top)
            if yt <= y < yt + h:
                return t, yt, h
        return None

    def set_view(self, start: float | None = None, spp: float | None = None) -> None:
        p = self.p
        if p is None:
            return
        if spp is not None:
            p.spp = max(MIN_SPP, min(self.fit_spp() * 8, float(spp)))
        if start is not None:
            end = max(p.frames, p.rate * 60) + self.width() * self.spp() * 0.5
            p.view_start = max(0.0, min(float(start), end - self.width() * self.spp() * 0.5))
        self.refresh()
        self.viewChanged.emit()

    def zoom(self, factor: float, anchor_x: float | None = None) -> None:
        if self.p is None:
            return
        ax = self.width() / 2 if anchor_x is None else anchor_x
        if anchor_x is None and self.p.cursor >= self.visible()[0] and self.p.cursor <= self.visible()[1]:
            ax = self.x_of(self.p.cursor)
        f = self.start() + ax * self.spp()
        spp = max(MIN_SPP, min(self.fit_spp() * 8, self.spp() * factor))
        self.set_view(f - ax * spp, spp)

    def zoom_all(self) -> None:
        if self.p is not None:
            self.set_view(0, self.fit_spp())

    def zoom_range(self, a: int, b: int) -> None:
        if b > a:
            spp = max(MIN_SPP, (b - a) * 1.04 / max(1, self.width()))
            self.set_view(a - (b - a) * 0.02, spp)

    def ensure_visible(self, frame: int, follow: bool = False) -> None:
        a, b = self.visible()
        if frame < a or frame > b - (b - a) * (0.1 if follow else 0.0):
            self.set_view(frame - (b - a) * (0.1 if follow else 0.5))

    def ensure_track_visible(self, tid: int) -> None:
        for t, top, h in lanes(self.p):
            if t.id == tid:
                if top < self.yoff:
                    self.set_yoff(top)
                elif top + h > self.yoff + self.view_h():
                    self.set_yoff(top + h - self.view_h())

    def set_play_pos(self, pos, follow: bool = False) -> None:
        self.play_pos = pos
        if pos is not None and follow and self._drag is None:
            a, b = self.visible()
            if pos > b - (b - a) * 0.05 or pos < a:
                self.set_view(pos - (b - a) * 0.05)
                return
        self.update()

    def current(self) -> int | None:
        return self.p.ui.get("current") if self.p is not None else None

    def sel_tracks(self) -> set:
        return set(self.p.ui.get("sel_tracks") or []) if self.p is not None else set()

    # ── snapping ──────────────────────────────────────────────
    def _grid(self) -> float:
        step, minor = nice_step(self.spp() / self.p.rate, 92)
        return step / minor * self.p.rate

    def snap(self, f: float, exclude=frozenset(), enabled: bool = True) -> tuple[int, bool]:
        """f onto the nearest clip edge / marker / cursor / grid line within SNAP_PX; → (frame, snapped)."""
        if not enabled or not self.p.ui.get("snap", True):
            return int(round(f)), False
        tol = SNAP_PX * self.spp()
        best, bd = None, tol
        for q in self.p.snap_points(exclude):
            d = abs(q - f)
            if d < bd:
                best, bd = q, d
        if best is None and self.p.ui.get("snap_grid", True):
            g = self._grid()
            q = round(f / g) * g
            if abs(q - f) < tol * 0.6:
                best = int(round(q))
        if best is None:
            return int(round(f)), False
        return int(best), True

    # ── painting ──────────────────────────────────────────────
    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        if self.p is None:
            p.fillRect(self.rect(), style.qcolor(style.W_BG))
            return
        key = (self.p.version, self.p.mversion, self.start(), self.spp(), self.width(), self.height(), self.yoff,
               style.THEME, tuple(sorted(self.p.sel_clips)), tuple(self.p.ui.get("env") or []),
               tuple(self.p.ui.get("sel_tracks") or []), self.p.ui.get("current"),
               tuple(m.offline for m in self.p.media.values()))
        if key != self._cache_key or self._cache is None or self._cache.size() != self.size() * \
                self.devicePixelRatioF():
            self._cache = self._render()
            self._cache_key = key
        p.drawPixmap(0, 0, self._cache)
        self._paint_overlay(p)

    def _render(self) -> QPixmap:
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(style.qcolor(style.W_BG))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, False)
        w = self.width()
        cur = self.current()
        sel_t = self.sel_tracks()
        grid = style.qcolor(style.W_GRID)
        # the grid under everything
        step, minor = nice_step(self.spp() / self.p.rate)
        a_sec, b_sec = self.visible()[0] / self.p.rate, self.visible()[1] / self.p.rate
        k = math.floor(a_sec / step)
        p.setPen(QPen(grid, 1))
        while k * step <= b_sec + step:
            x = self.x_of(k * step * self.p.rate)
            p.drawLine(QPointF(x, RULER_H), QPointF(x, self.height()))
            k += 1
        for t, top, h in lanes(self.p):
            y = self.y_top(top)
            if y > self.height() or y + h < RULER_H:
                continue
            lane = QRectF(0, y, w, h)
            if t.id == cur or t.id in sel_t:
                c = style.qcolor(style.W_SEL)
                c.setAlpha(70 if style.IS_DARK else 90)
                p.fillRect(lane, c)
            if t.kind == "bus":
                c = style.qcolor(style.PANEL2)
                c.setAlpha(120)
                p.fillRect(lane, c)
                p.setPen(style.qcolor(style.MUTED))
                p.setFont(_font(11))
                p.drawText(lane.adjusted(10, 0, -10, 0), Qt.AlignLeft | Qt.AlignVCenter, t("mt_bus_lane"))
            for c in t.clips:
                self._paint_clip(p, t, c, y, h)
            if t.id in (self.p.ui.get("env") or []):
                self._paint_env(p, t, y, h)
            p.setPen(QPen(style.qcolor(style.BORDER), 1))
            p.drawLine(QPointF(0, y + h - 0.5), QPointF(w, y + h - 0.5))
        self._paint_ruler(p)
        p.end()
        return pm

    def clip_rect(self, c, y: float, h: float) -> QRectF:
        x1, x2 = self.x_of(c.start), self.x_of(c.end)
        return QRectF(x1, y + 2, max(2.0, x2 - x1), h - 5)

    def _paint_clip(self, p: QPainter, t, c, y: float, h: float) -> None:
        r = self.clip_rect(c, y, h)
        if r.right() < 0 or r.left() > self.width():
            return
        col = track_color(t.color)
        m = self.p.media.get(c.media)
        offline = m is None or m.src is None
        sel = c.id in self.p.sel_clips
        body = QColor(col)
        body.setAlpha(55 if style.IS_DARK else 45)
        if c.muted:
            body.setAlpha(18)
        if offline:
            body = QColor(style.W_CLIP)
            body.setAlpha(60)
        p.fillRect(r, body)
        head = QRectF(r.left(), r.top(), r.width(), CLIP_HEAD)
        hc = QColor(col)
        hc.setAlpha(200 if sel else 120)
        p.fillRect(head, hc)
        # the wave
        vis_a = max(r.left(), 0.0)
        vis_b = min(r.right(), float(self.width()))
        wave = QRectF(vis_a, r.top() + CLIP_HEAD, vis_b - vis_a, r.height() - CLIP_HEAD)
        if not offline and wave.width() > 1 and wave.height() > 4:
            ncols = int(math.ceil(wave.width()))
            f0 = self.start() + vis_a * self.spp() - c.start + c.offset
            cols = self._peaks.columns(m, f0, self.spp(), ncols)
            if cols is not None:
                g = 0.0 if c.gain_db <= -60 else 10 ** (c.gain_db / 20)
                xs = vis_a + np.arange(ncols)
                rel = (self.start() + xs * self.spp()) - c.start
                env = np.ones(ncols)
                if c.fade_in:
                    env *= np.where(rel < c.fade_in, curve_gain(c.curve_in, rel / c.fade_in), 1.0)
                if c.fade_out:
                    env *= np.where(rel > c.length - c.fade_out,
                                    curve_gain(c.curve_out, (c.length - rel) / c.fade_out), 1.0)
                lo = np.clip(np.nan_to_num(cols[:, 0]) * g * env, -1, 1)
                hi = np.clip(np.nan_to_num(cols[:, 1]) * g * env, -1, 1)
                mid = wave.center().y()
                half = wave.height() / 2 - 1
                img = QImage(ncols, max(1, int(wave.height())), QImage.Format_ARGB32_Premultiplied)
                img.fill(0)
                ip = QPainter(img)
                wc = QColor(style.W_WAVE_SEL if sel else style.W_WAVE) if not c.muted else style.qcolor(style.MUTED)
                ip.setPen(QPen(wc, 1))
                hh = img.height() / 2
                for i in range(ncols):
                    if np.isnan(cols[i, 0]):
                        continue
                    y1, y2 = hh - hi[i] * (hh - 1), hh - lo[i] * (hh - 1)
                    ip.drawLine(QPointF(i + 0.5, y1), QPointF(i + 0.5, max(y2, y1 + 1)))
                ip.end()
                p.drawImage(QPointF(vis_a, wave.top()), img)
                del mid, half
        # fades
        p.setRenderHint(QPainter.Antialiasing, True)
        shade = QColor(0, 0, 0, 90 if style.IS_DARK else 50)
        line = QColor("#ffffff" if style.IS_DARK else "#202020")
        for which in ("in", "out"):
            n = c.fade_in if which == "in" else c.fade_out
            if not n:
                continue
            curve = c.curve_in if which == "in" else c.curve_out
            xa = self.x_of(c.start if which == "in" else c.end - n)
            xb = self.x_of(c.start + n if which == "in" else c.end)
            steps = max(2, min(64, int(xb - xa)))
            top, bot = r.top() + CLIP_HEAD, r.bottom()
            pts = []
            for i in range(steps + 1):
                u = i / steps
                gv = float(curve_gain(curve, np.array([u if which == "in" else 1 - u]))[0])
                pts.append(QPointF(xa + (xb - xa) * u, bot - (bot - top) * gv))
            path = QPainterPath(pts[0])
            for q in pts[1:]:
                path.lineTo(q)
            fill = QPainterPath(path)
            fill.lineTo(QPointF(xb, top) if which == "in" else QPointF(xb, top))
            fill.lineTo(QPointF(xa, top))
            fill.closeSubpath()
            p.fillPath(fill, shade)
            p.setPen(QPen(line, 1))
            p.drawPath(path)
        p.setRenderHint(QPainter.Antialiasing, False)
        # the frame, the name, the handles
        p.setPen(QPen(style.qcolor(style.ACCENT) if sel else QColor(col).darker(130), 2 if sel else 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
        p.setFont(_font(10, True))
        p.setPen(QColor("#ffffff") if style.IS_DARK else QColor("#101010"))
        name = c.name or (m.name if m is not None else "?")
        extra = []
        if len(c.takes) > 1:
            extra.append(t("mt_take_n", c.take + 1, len(c.takes)))
        if abs(c.gain_db) > 0.05:
            extra.append(f"{c.gain_db:+.1f} dB".replace("-", "−"))
        if offline:
            extra.append(t("mt_offline"))
        if c.muted:
            extra.append(t("mt_muted"))
        text = name + ("  · " + " · ".join(extra) if extra else "")
        fm = QFontMetrics(p.font())
        tr = head.adjusted(HANDLE + 3, 0, -HANDLE - 3, 0)
        if tr.left() < 0 < tr.right():
            tr.setLeft(3)
        p.drawText(tr, Qt.AlignLeft | Qt.AlignVCenter, fm.elidedText(text, Qt.ElideRight, max(0, int(tr.width()))))
        if r.width() > 3 * HANDLE:
            p.setBrush(QColor("#ffffff"))
            p.setPen(QPen(QColor("#000000"), 1))
            for hr in self._handles(c, r).values():
                p.drawRect(hr)
            p.setBrush(Qt.NoBrush)

    def _handles(self, c, r: QRectF) -> dict:
        fi = self.x_of(c.start + c.fade_in)
        fo = self.x_of(c.end - c.fade_out)
        y = r.top() + CLIP_HEAD + 1
        out = {"fade_in": QRectF(min(fi, r.right() - HANDLE) - HANDLE / 2 + (HANDLE / 2 if not c.fade_in else 0), y,
                                 HANDLE, HANDLE),
               "fade_out": QRectF(max(fo, r.left() + HANDLE) - HANDLE / 2 - (HANDLE / 2 if not c.fade_out else 0), y,
                                  HANDLE, HANDLE)}
        gy = r.top() + CLIP_HEAD + 1
        out["gain"] = QRectF(r.center().x() - HANDLE / 2, gy, HANDLE, HANDLE)
        return out

    def env_y(self, which: str, v: float, y: float, h: float) -> float:
        top, bot = y + CLIP_HEAD + 4, y + h - 6
        if which == "vol":
            u = (v - ENV_MIN_DB) / (ENV_MAX_DB - ENV_MIN_DB)
            u = math.sqrt(max(0.0, min(1.0, u)))
        else:
            u = (v + 1) / 2
        return bot - (bot - top) * u

    def env_value_at(self, which: str, yy: float, y: float, h: float) -> float:
        top, bot = y + CLIP_HEAD + 4, y + h - 6
        u = max(0.0, min(1.0, (bot - yy) / max(1.0, bot - top)))
        if which == "vol":
            return ENV_MIN_DB + (ENV_MAX_DB - ENV_MIN_DB) * u * u
        return u * 2 - 1

    def _paint_env(self, p: QPainter, t, y: float, h: float) -> None:
        p.setRenderHint(QPainter.Antialiasing, True)
        for which, env, default, col in (("vol", t.vol_env, 0.0, "#ffd43b"), ("pan", t.pan_env, t.pan, "#4dabf7")):
            c = QColor(col)
            if not t.env_on:
                c.setAlpha(110)
            p.setPen(QPen(c, 1.5, Qt.SolidLine if env else Qt.DashLine))
            pts = env or ((0, default),)
            a, b = self.visible()
            path = QPainterPath()
            first = True
            xs = [(a, env_value(pts, a, default))] + [(f, v) for f, v in pts if a <= f <= b] + \
                [(b, env_value(pts, b, default))]
            for f, v in xs:
                q = QPointF(self.x_of(f), self.env_y(which, v, y, h))
                if first:
                    path.moveTo(q)
                    first = False
                else:
                    path.lineTo(q)
            p.drawPath(path)
            p.setBrush(c)
            for f, v in env:
                if a <= f <= b:
                    p.drawEllipse(QPointF(self.x_of(f), self.env_y(which, v, y, h)), 3.5, 3.5)
            p.setBrush(Qt.NoBrush)
        p.setRenderHint(QPainter.Antialiasing, False)

    def _paint_ruler(self, p: QPainter) -> None:
        rate = self.p.rate
        w = self.width()
        p.fillRect(QRectF(0, 0, w, RULER_H), style.qcolor(style.W_RULER))
        p.fillRect(QRectF(0, TIME_H, w, MARK_H), style.qcolor(style.W_BG))
        p.setPen(QPen(style.qcolor(style.BORDER), 1))
        p.drawLine(QPointF(0, RULER_H - 0.5), QPointF(w, RULER_H - 0.5))
        p.drawLine(QPointF(0, TIME_H - 0.5), QPointF(w, TIME_H - 0.5))
        step, minor = nice_step(self.spp() / rate)
        a_sec, b_sec = self.visible()[0] / rate, self.visible()[1] / rate
        sub = step / minor
        k = math.floor(a_sec / sub)
        p.setFont(_font(11))
        text = style.qcolor(style.W_RULER_TEXT)
        while k * sub <= b_sec + sub:
            sec = k * sub
            x = self.x_of(sec * rate)
            major = abs(round(sec / step) * step - sec) < sub * 0.01
            p.setPen(QPen(text, 1))
            p.drawLine(QPointF(x, TIME_H - (9 if major else 4)), QPointF(x, TIME_H - 1))
            if major:
                p.drawText(QRectF(x + 3, 2, 120, TIME_H - 9), Qt.AlignLeft | Qt.AlignVCenter, ruler_label(sec, step))
            k += 1
        # markers and regions in the strip
        green = style.qcolor(style.W_MARKER)
        f = _font(10, True)
        fm = QFontMetrics(f)
        p.setFont(f)
        for m in self.shown_markers():
            if m.end is not None:
                x1, x2 = self.x_of(m.pos), self.x_of(m.end)
                fill = QColor(green)
                fill.setAlpha(70)
                bar = QRectF(x1, TIME_H + 1, max(2.0, x2 - x1), MARK_H - 2)
                p.fillRect(bar, fill)
                p.setPen(QPen(green, 1, Qt.DashLine))
                for x in (x1, x2):
                    p.drawLine(QPointF(x + 0.5, TIME_H), QPointF(x + 0.5, self.height()))
                p.setPen(style.qcolor(style.TEXT))
                p.drawText(bar.adjusted(4, 0, -2, 0), Qt.AlignLeft | Qt.AlignVCenter,
                           fm.elidedText(m.name, Qt.ElideRight, max(0, int(bar.width()) - 6)))
        for m in self.shown_markers():
            if m.end is None:
                x = self.x_of(m.pos)
                p.setPen(QPen(green, 1))
                p.drawLine(QPointF(x + 0.5, TIME_H), QPointF(x + 0.5, self.height()))
                r = QRectF(x, TIME_H + 1, min(160, fm.horizontalAdvance(m.name or "·") + 8), MARK_H - 2)
                p.fillRect(r, green)
                p.setPen(QColor("#0b1f12") if style.IS_DARK else QColor("#ffffff"))
                p.drawText(r.adjusted(4, 0, -2, 0), Qt.AlignLeft | Qt.AlignVCenter,
                           fm.elidedText(m.name, Qt.ElideRight, int(r.width()) - 6))

    def shown_markers(self) -> list:
        d = self._drag
        if d and d["kind"] == "marker" and d.get("moved"):
            from ...doc import Marker
            return [Marker(m.id, m.name, d["pos"], d["end"]) if m.id == d["id"] else m for m in self.p.markers]
        return list(self.p.markers)

    def _paint_overlay(self, p: QPainter) -> None:
        pr = self.p
        h = self.height()
        if pr.sel:
            x1, x2 = self.x_of(pr.sel[0]), self.x_of(pr.sel[1])
            x2 = max(x2, x1 + 1)
            c = style.qcolor(style.W_SEL)
            c.setAlpha(110)
            sel_t = self.sel_tracks()
            p.setCompositionMode(QPainter.CompositionMode_Screen if style.IS_DARK else
                                 QPainter.CompositionMode_Multiply)
            if sel_t:
                for t, top, lh in lanes(pr):
                    if t.id in sel_t:
                        y = self.y_top(top)
                        p.fillRect(QRectF(x1, max(RULER_H, y), x2 - x1, lh), c)
            else:
                p.fillRect(QRectF(x1, RULER_H, x2 - x1, h - RULER_H), c)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.fillRect(QRectF(x1, TIME_H - 5, x2 - x1, 5), style.qcolor(style.ACCENT))
        for tid, (s, n) in self.rec_preview.items():
            for t, top, lh in lanes(pr):
                if t.id == tid:
                    y = self.y_top(top)
                    r = QRectF(self.x_of(s), y + 2, max(2.0, n / self.spp()), lh - 5)
                    c = QColor(style.W_CLIP)
                    c.setAlpha(90)
                    p.fillRect(r, c)
                    p.setPen(QPen(QColor(style.W_CLIP), 1))
                    p.drawRect(r)
        xc = self.x_of(pr.cursor)
        p.setPen(QPen(style.qcolor(style.W_CURSOR), 1, Qt.DashLine))
        p.drawLine(QPointF(xc + 0.5, TIME_H), QPointF(xc + 0.5, h))
        p.setPen(Qt.NoPen)
        p.setBrush(style.qcolor(style.W_CURSOR))
        p.drawPolygon(QPolygonF([QPointF(xc - 5, TIME_H - 7), QPointF(xc + 5, TIME_H - 7),
                                 QPointF(xc + 0.5, TIME_H - 1)]))
        p.setBrush(Qt.NoBrush)
        if self._snap_x is not None:
            p.setPen(QPen(style.qcolor(style.ACCENT), 1))
            p.drawLine(QPointF(self._snap_x, RULER_H), QPointF(self._snap_x, h))
        if self.play_pos is not None:
            xp = self.x_of(self.play_pos)
            p.setPen(QPen(style.qcolor(style.W_PLAY), 2))
            p.drawLine(QPointF(xp, 0), QPointF(xp, h))
        if not pr.tracks:
            p.setPen(style.qcolor(style.MUTED))
            p.setFont(_font(13))
            p.drawText(QRectF(0, RULER_H, self.width(), h - RULER_H), Qt.AlignCenter, t("mt_empty_hint"))

    # ── hit testing ───────────────────────────────────────────
    def hit(self, x: float, y: float):
        """What is under the mouse: (kind, track, clip, extra) — kind marker | ruler | env | fade_in | fade_out |
        gain | left | right | clip | lane | none."""
        pr = self.p
        if pr is None:
            return "none", None, None, None
        if y < TIME_H:
            return "ruler", None, None, None
        if y < RULER_H:
            m, part = self._marker_at(x)
            return ("marker", None, None, (m, part)) if m is not None else ("ruler", None, None, None)
        ln = self.lane_at(y)
        if ln is None:
            return "none", None, None, None
        t, yt, lh = ln
        if t.id in (pr.ui.get("env") or []):
            for which, env, default in (("vol", t.vol_env, 0.0), ("pan", t.pan_env, t.pan)):
                for i, (f, v) in enumerate(env):
                    if abs(self.x_of(f) - x) <= 5 and abs(self.env_y(which, v, yt, lh) - y) <= 5:
                        return "env", t, None, (which, i)
            for which, env, default in (("vol", t.vol_env, 0.0), ("pan", t.pan_env, t.pan)):
                v = env_value(env, self.frame_at(x), default)
                if abs(self.env_y(which, v, yt, lh) - y) <= 4:
                    return "env_line", t, None, (which, None)
        for c in reversed(t.clips):
            r = self.clip_rect(c, yt, lh)
            if not (r.left() - 2 <= x <= r.right() + 2 and r.top() <= y <= r.bottom()):
                continue
            if r.width() > 3 * HANDLE:
                for k, hr in self._handles(c, r).items():
                    if hr.adjusted(-2, -2, 2, 2).contains(QPointF(x, y)):
                        return k, t, c, None
            if y > r.top() + CLIP_HEAD and x - r.left() <= EDGE_PX and r.width() > 2 * EDGE_PX:
                return "left", t, c, None
            if y > r.top() + CLIP_HEAD and r.right() - x <= EDGE_PX and r.width() > 2 * EDGE_PX:
                return "right", t, c, None
            return "clip", t, c, None
        return "lane", t, None, None

    def _marker_at(self, x: float):
        fm = QFontMetrics(_font(10, True))
        for m in reversed(self.p.markers):
            if m.end is None:
                xm = self.x_of(m.pos)
                if xm - 3 <= x <= xm + min(160, fm.horizontalAdvance(m.name or "·") + 8):
                    return m, "pos"
        for m in self.p.markers:
            if m.end is None:
                continue
            x1, x2 = self.x_of(m.pos), self.x_of(m.end)
            if abs(x - x1) <= 4:
                return m, "start"
            if abs(x - x2) <= 4:
                return m, "end"
            if x1 < x < x2:
                return m, "move"
        return None, ""

    # ── the mouse ─────────────────────────────────────────────
    def _cursor_shape(self, kind: str):
        return {"left": Qt.SizeHorCursor, "right": Qt.SizeHorCursor, "fade_in": Qt.SizeHorCursor,
                "fade_out": Qt.SizeHorCursor, "gain": Qt.SizeVerCursor, "env": Qt.PointingHandCursor,
                "env_line": Qt.CrossCursor, "marker": Qt.SizeHorCursor, "clip": Qt.OpenHandCursor}.get(kind,
                                                                                                     Qt.ArrowCursor)

    def mousePressEvent(self, ev) -> None:
        pr = self.p
        if pr is None:
            return
        x, y = ev.position().x(), ev.position().y()
        kind, tr, c, extra = self.hit(x, y)
        mods = ev.modifiers()
        if ev.button() == Qt.RightButton:
            if c is not None and c.id not in pr.sel_clips:
                pr.sel_clips = frozenset({c.id})
            if tr is not None:
                pr.ui["current"] = tr.id
            self.refresh()
            self.cursorMoved.emit()
            self.contextRequested.emit(ev.globalPosition().toPoint(), tr, self.frame_at(x), c)
            return
        if ev.button() != Qt.LeftButton:
            return
        f = self.frame_at(x)
        self._drag = {"kind": kind, "x0": x, "y0": y, "f0": f, "moved": False, "base": (pr.tracks, pr.markers),
                      "pos_before": pr.history[pr.pos].id}
        if tr is not None:
            pr.ui["current"] = tr.id
        if kind == "marker":
            m, part = extra
            self._drag.update(kind="marker", id=m.id, part=part, pos=m.pos, end=m.end, m0=(m.pos, m.end))
        elif kind in ("ruler", "lane", "none"):
            if kind == "lane" and not (mods & Qt.ControlModifier):
                pr.sel_clips = frozenset()
            snap_f, _s = self.snap(f, enabled=not (mods & Qt.ShiftModifier))
            if mods & Qt.ShiftModifier and pr.sel:
                a = pr.sel[0] if abs(f - pr.sel[0]) > abs(f - pr.sel[1]) else pr.sel[1]
                self._drag.update(kind="select", anchor=a, t0=tr.id if tr else None)
                self._set_sel(a, f, tr)
            else:
                pr.cursor = snap_f
                pr.sel = None
                pr.ui["sel_tracks"] = [tr.id] if tr is not None else []
                self._drag.update(kind="select", anchor=snap_f, t0=tr.id if tr else None)
                self.seekRequested.emit(snap_f)
        elif kind in ("clip", "left", "right", "fade_in", "fade_out", "gain"):
            if mods & Qt.ControlModifier and kind == "clip":
                sel = set(pr.sel_clips)
                sel.symmetric_difference_update({c.id})
                pr.sel_clips = frozenset(sel)
            elif c.id not in pr.sel_clips:
                pr.sel_clips = frozenset({c.id})
            self._drag.update(clip=c.id, c0=c, track=tr.id, copy=bool(mods & Qt.ControlModifier) and kind == "clip")
            if kind == "clip":
                self.setCursor(Qt.ClosedHandCursor)
        elif kind == "env":
            which, i = extra
            self._drag.update(track=tr.id, which=which, index=i)
        elif kind == "env_line":
            which, _i = extra
            ln = self.lane_at(y)
            v = self.env_value_at(which, y, ln[1], ln[2])
            env = list(tr.vol_env if which == "vol" else tr.pan_env)
            if not env:
                env = [(0, 0.0 if which == "vol" else tr.pan)]
            env.append((f, v))
            pr.set_envelope(tr.id, which, env, t("mt_u_env"))
            nt = pr.track(tr.id)
            env = nt.vol_env if which == "vol" else nt.pan_env
            idx = next((k for k, (ff, _v) in enumerate(env) if ff == f), 0)
            self._drag.update(kind="env", track=tr.id, which=which, index=idx, base=(pr.tracks, pr.markers))
            self.edited.emit()
        self.refresh()
        self.cursorMoved.emit()

    def _set_sel(self, a: int, f: int, tr) -> None:
        pr = self.p
        lo, hi = sorted((a, f))
        pr.sel = (lo, hi) if hi > lo else None
        t0 = self._drag.get("t0") if self._drag else None
        if tr is not None and t0 is not None:
            ids = [t.id for t in pr.tracks]
            i0, i1 = sorted((ids.index(t0), ids.index(tr.id))) if t0 in ids and tr.id in ids else (0, -1)
            pr.ui["sel_tracks"] = ids[i0:i1 + 1]

    def mouseMoveEvent(self, ev) -> None:
        pr = self.p
        if pr is None:
            return
        x, y = ev.position().x(), ev.position().y()
        d = self._drag
        if d is None:
            kind, *_ = self.hit(x, y)
            self.setCursor(self._cursor_shape(kind))
            return
        if not d["moved"] and abs(x - d["x0"]) < 3 and abs(y - d["y0"]) < 3:
            return
        d["moved"] = True
        nosnap = bool(ev.modifiers() & Qt.ShiftModifier)
        f = self.frame_at(x)
        k = d["kind"]
        self._snap_x = None
        if k == "select":
            ln = self.lane_at(y)
            sf, ok = self.snap(f, enabled=not nosnap)
            if ok:
                self._snap_x = self.x_of(sf)
            self._set_sel(d["anchor"], sf, ln[0] if ln else None)
            pr.cursor = min(d["anchor"], sf)
            self._edge_scroll(x)
            self.update()
            self.cursorMoved.emit()
            return
        if k == "marker":
            m0p, m0e = d["m0"]
            sf, ok = self.snap(f, enabled=not nosnap)
            if d["part"] == "pos":
                d["pos"] = sf
            elif d["part"] == "start":
                d["pos"] = min(sf, m0e - 1)
            elif d["part"] == "end":
                d["end"] = max(sf, m0p + 1)
            else:
                delta = sf - d["f0"]
                d["pos"], d["end"] = max(0, m0p + delta), max(0, m0p + delta) + (m0e - m0p)
            self.refresh()
            return
        tracks, _ms = d["base"]
        if k in ("clip", "left", "right", "fade_in", "fade_out", "gain", "env"):
            pr.tracks = tracks                           # from where the drag began: one step, any path
        if k == "clip":
            c0 = d["c0"]
            delta = f - d["f0"]
            new_start = c0.start + delta
            ids = set(pr.sel_clips) | {c0.id}
            if not nosnap:
                s1, ok1 = self.snap(new_start, frozenset(ids))
                s2, ok2 = self.snap(new_start + c0.length, frozenset(ids))
                if ok1 and (not ok2 or abs(s1 - new_start) <= abs(s2 - new_start - c0.length)):
                    delta = s1 - c0.start
                    self._snap_x = self.x_of(s1)
                elif ok2:
                    delta = s2 - c0.length - c0.start
                    self._snap_x = self.x_of(s2)
            ln = self.lane_at(y)
            audio = [t for t in pr.tracks if t.kind == "audio"]
            ti = next((i for i, t in enumerate(audio) if t.id == d["track"]), 0)
            tj = next((i for i, t in enumerate(audio) if ln and t.id == ln[0].id), ti) if ln else ti
            new_ids = pr.move_clips(ids, delta, tj - ti, t("mt_u_copy") if d["copy"] else t("mt_u_move"),
                                    copy=d["copy"], merge="drag")
            if d["copy"]:
                d["copied"] = new_ids
            self._edge_scroll(x)
        elif k in ("left", "right"):
            sf, ok = self.snap(f, frozenset({d["clip"]}), enabled=not nosnap)
            if ok:
                self._snap_x = self.x_of(sf)
            c0 = d["c0"]
            pr.trim_clip(c0.id, k, sf, t("mt_u_trim"), merge="drag", media_frames=self.media_frames(c0.media))
        elif k in ("fade_in", "fade_out"):
            c0 = d["c0"]
            n = (f - c0.start) if k == "fade_in" else (c0.end - f)
            pr.set_clip(c0.id, t("mt_u_fade"), merge="drag", **{k: max(0, n)})
        elif k == "gain":
            c0 = d["c0"]
            db = c0.gain_db + (d["y0"] - y) * 0.25
            if not nosnap and abs(db) < 0.6:
                db = 0.0
            pr.set_clip(c0.id, t("mt_u_gain"), merge="drag", gain_db=round(db, 1))
            self.setToolTip(f"{db:+.1f} dB")
        elif k == "env":
            tr = pr.track(d["track"])
            ln = next(((top, h) for t, top, h in lanes(pr) if t.id == tr.id), (0, LANE_H))
            v = self.env_value_at(d["which"], y, self.y_top(ln[0]), ln[1])
            env = list(tr.vol_env if d["which"] == "vol" else tr.pan_env)
            if 0 <= d["index"] < len(env):
                sf, _ok = self.snap(f, enabled=not nosnap)
                lo = env[d["index"] - 1][0] + 1 if d["index"] > 0 else 0
                hi = env[d["index"] + 1][0] - 1 if d["index"] + 1 < len(env) else 10 ** 12
                env[d["index"]] = (max(lo, min(hi, sf)), v)
                pr.set_envelope(tr.id, d["which"], env, t("mt_u_env"), merge="drag")
        self.refresh()
        self.edited.emit()

    def _edge_scroll(self, x: float) -> None:
        self._edge = x
        if x < 0 or x > self.width():
            if not self._scroll_timer.isActive():
                self._scroll_timer.start()
        else:
            self._scroll_timer.stop()

    def _auto_scroll(self) -> None:
        if self._drag is None:
            self._scroll_timer.stop()
            return
        x = getattr(self, "_edge", 0)
        a, b = self.visible()
        step = (b - a) * 0.04
        if x < 0:
            self.set_view(self.start() - step)
        elif x > self.width():
            self.set_view(self.start() + step)

    def mouseReleaseEvent(self, ev) -> None:
        d, self._drag = self._drag, None
        self._scroll_timer.stop()
        self._snap_x = None
        self.setToolTip("")
        self.unsetCursor()
        if d is None or self.p is None:
            return
        pr = self.p
        if d["kind"] == "marker" and d["moved"]:
            pr.update_marker(d["id"], pos=d["pos"], end=d["end"], label=t("mt_u_marker"))
            self.edited.emit()
        elif d["kind"] == "marker":
            m = pr.marker(d["id"])
            if m is not None:
                pr.cursor = m.pos
                if m.end is not None:
                    pr.sel = (m.pos, m.end)
                self.seekRequested.emit(m.pos)
        elif d["kind"] in ("clip", "left", "right", "fade_in", "fade_out", "gain", "env") and d["moved"]:
            if pr.history and pr.history[pr.pos].merge == "drag":
                pr.history[pr.pos].merge = ""           # the next drag is a step of its own
            self.edited.emit()
        elif d["kind"] == "clip" and not d["moved"]:
            c = d["c0"]
            if not (ev.modifiers() & Qt.ControlModifier):
                pr.sel_clips = frozenset({c.id})
            pr.cursor = self.frame_at(d["x0"])
            self.seekRequested.emit(pr.cursor)
        elif d["kind"] == "select" and not d["moved"]:
            pass
        self.refresh()
        self.cursorMoved.emit()

    def mouseDoubleClickEvent(self, ev) -> None:
        x, y = ev.position().x(), ev.position().y()
        kind, tr, c, extra = self.hit(x, y)
        if kind == "env" and tr is not None:
            which, i = extra
            env = list(tr.vol_env if which == "vol" else tr.pan_env)
            if 0 <= i < len(env):
                del env[i]
                self.p.set_envelope(tr.id, which, env, t("mt_u_env"))
                self.refresh()
                self.edited.emit()
            return
        if kind == "marker":
            return
        if c is not None:
            self.openClip.emit(c.id)

    def wheelEvent(self, ev) -> None:
        if self.p is None:
            return
        dy = ev.angleDelta().y()
        dx = ev.angleDelta().x()
        mods = ev.modifiers()
        if mods & Qt.ControlModifier:
            if dy:
                self.zoom(0.8 ** (dy / 120), ev.position().x())
        elif mods & Qt.ShiftModifier or dx:
            steps = (dx or dy) / 120
            self.set_view(self.start() - steps * self.width() * self.spp() * 0.15)
        else:
            self.set_yoff(self.yoff - int(dy / 120 * 60))
        ev.accept()

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self.refresh()
        self.viewChanged.emit()

    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev) -> None:
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if not paths:
            return
        pos = ev.position()
        ln = self.lane_at(pos.y())
        f, _ok = self.snap(self.frame_at(pos.x()))
        self.filesDropped.emit(paths, ln[0].id if ln and ln[0].kind == "audio" else None, f)
        ev.acceptProposedAction()

    def tick_rec(self) -> None:
        self.update()

