"""The video window (2.0, Film → Video, F8): the picture of the film the document came from, at the document's time.

Stopped: the frame at the edit cursor (an exact seek). Playing: the film plays along, following our player's clock —
libmpv (as in ArtCore Player and Convert) renders into an OpenGL widget with its own sound off; every 50 ms the
picture's time is compared with what is heard: a small drift is taken up by the speed (±2 %), a big one (a jump, a
loop) by an exact seek. Without libmpv the window shows still frames from ffmpeg (a few a second while playing).

The window is a window of its own: it can go to a second monitor; its place and size are remembered."""
from __future__ import annotations

import locale
import os
import sys
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QOpenGLContext, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QStackedLayout, QVBoxLayout, QWidget

from . import ff
from .i18n import t
from .winint import IS_WIN

_mpv = None
_mpv_error = ""


def prepare_libmpv() -> list[str]:
    """libmpv-2.dll next to the exe, in PyInstaller's _internal, or ./libmpv of the source tree."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dirs = []
    for d in (os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else "", getattr(sys, "_MEIPASS", ""),
              root, os.path.join(root, "libmpv")):
        if d and os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    if IS_WIN:
        os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])
        for d in dirs:
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
    return dirs


def load_mpv():
    """The python-mpv module, or None (the reason in mpv_error())."""
    global _mpv, _mpv_error
    if _mpv is not None or _mpv_error:
        return _mpv
    if os.environ.get("ARTCORE_NO_MPV"):
        _mpv_error = "disabled"
        return None
    try:
        prepare_libmpv()
        locale.setlocale(locale.LC_NUMERIC, "C")            # libmpv refuses a non-C LC_NUMERIC
        import mpv
        _mpv = mpv
    except Exception as e:                                   # OSError: no libmpv; ImportError: no python-mpv
        _mpv_error = str(e) or e.__class__.__name__
    return _mpv


def mpv_error() -> str:
    return _mpv_error


# ── following the player's clock ──────────────────────────────
class Follower:
    """What the picture should do, given the time heard (s), whether we play, and the picture's own time / state.
    Returns a list of ("seek", t) / ("play", speed) / ("pause",) — no Qt, no mpv (tests drive it)."""
    SEEK_OVER = 0.08          # further than this from the sound: seek
    NUDGE_OVER = 0.012        # … further than this: correct by the speed
    MAX_NUDGE = 0.02          # at most ±2 %

    def __init__(self, fps: float = 25.0):
        self.fps = fps or 25.0
        self.last_seek = None
        self.speed = 1.0

    def step(self, audio_t: float, playing: bool, video_t: float | None, video_paused: bool) -> list:
        out = []
        if not playing:
            if not video_paused:
                out.append(("pause",))
                self.speed = 1.0
            frame = 1.0 / self.fps
            if self.last_seek is None or abs(audio_t - self.last_seek) >= frame * 0.5:
                if video_t is None or abs(video_t - audio_t) >= frame * 0.5 or self.last_seek is None:
                    out.append(("seek", audio_t))
                    self.last_seek = audio_t
            return out
        self.last_seek = None
        if video_t is None or video_paused:
            out += [("seek", audio_t), ("play", 1.0)]
            self.speed = 1.0
            return out
        diff = video_t - audio_t                        # > 0: the picture is ahead
        if abs(diff) > self.SEEK_OVER:
            out += [("seek", audio_t), ("play", 1.0)]
            self.speed = 1.0
        elif abs(diff) > self.NUDGE_OVER:
            sp = 1.0 - max(-self.MAX_NUDGE, min(self.MAX_NUDGE, diff * 0.5))
            if abs(sp - self.speed) > 1e-4:
                out.append(("play", sp))
                self.speed = sp
        elif self.speed != 1.0:
            out.append(("play", 1.0))
            self.speed = 1.0
        return out


# ── the libmpv picture ────────────────────────────────────────
class _Bridge(QObject):
    prop = Signal(str, object)


def _gl_widget_class():
    from PySide6.QtOpenGLWidgets import QOpenGLWidget

    class GLView(QOpenGLWidget):
        frame = Signal()
        ready = Signal()

        def __init__(self, mpvmod, player, parent=None):
            super().__init__(parent)
            self._mpv, self.player = mpvmod, player
            self.ctx = None
            self._gpa = None
            self.paints = 0
            self.setAttribute(Qt.WA_OpaquePaintEvent)
            self.frame.connect(self.update, Qt.QueuedConnection)
            self.frameSwapped.connect(self._swapped)

        def initializeGL(self):
            ctx = QOpenGLContext.currentContext()

            def gpa(_, name):
                addr = ctx.getProcAddress(name.decode() if isinstance(name, bytes) else name)
                return int(addr) if addr else 0
            self._gpa = self._mpv.MpvGlGetProcAddressFn(gpa)
            self.ctx = self._mpv.MpvRenderContext(self.player, "opengl", opengl_init_params={"get_proc_address":
                                                                                              self._gpa})
            self.ctx.update_cb = self._on_update
            self.ready.emit()

        def _on_update(self):
            try:
                self.frame.emit()
            except RuntimeError:
                pass

        def paintGL(self):
            self.paints += 1
            if self.ctx is None:
                f = self.context().functions()
                f.glClearColor(0, 0, 0, 1)
                f.glClear(0x4000)
                return
            r = self.devicePixelRatioF()
            self.ctx.render(flip_y=True, opengl_fbo={"w": int(self.width() * r), "h": int(self.height() * r),
                                                     "fbo": self.defaultFramebufferObject()})

        def _swapped(self):
            if self.ctx is not None:
                try:
                    self.ctx.report_swap()
                except Exception:
                    pass

        def shutdown(self):
            if self.ctx is not None:
                self.makeCurrent()
                try:
                    self.ctx.update_cb = None
                    self.ctx.free()
                except Exception:
                    pass
                self.ctx = None
                self.doneCurrent()
    return GLView


class MpvPicture(QWidget):
    """libmpv without sound: load(path), seek(t), play(speed), pause(); pos / paused follow mpv."""

    def __init__(self, mpvmod, parent=None):
        super().__init__(parent)
        self.mpvmod = mpvmod
        self.pos = None
        self.paused = True
        self.path = ""
        self._pending = ""
        self._b = _Bridge()
        self._b.prop.connect(self._on_prop, Qt.QueuedConnection)
        self.player = mpvmod.MPV(vo="libmpv", hwdec="auto-safe", keep_open="yes", idle="yes", pause=True, osc=False,
                                 aid="no", audio="no", input_default_bindings=False, input_vo_keyboard=False,
                                 config=False, terminal=False, ytdl=False, hr_seek="yes", osd_level=0, sub_auto="no")
        for name in ("time-pos", "pause"):
            self.player.observe_property(name, lambda n, v: self._b.prop.emit(n, v))
        self.gl = _gl_widget_class()(mpvmod, self.player, self)
        self.gl.ready.connect(lambda: self._pending and self.load(self._pending))
        QStackedLayout(self).addWidget(self.gl)

    def _on_prop(self, name, v):
        if name == "time-pos":
            self.pos = None if v is None else float(v)
        elif name == "pause" and v is not None:
            self.paused = bool(v)

    def load(self, path: str) -> None:
        if self.gl.ctx is None:
            self._pending = path
            return
        self._pending = ""
        if path != self.path:
            self.path = path
            self.pos = None
            self.player.pause = True
            self.player.play(path)

    def unload(self) -> None:
        self.path = ""
        try:
            self.player.command("stop")
        except Exception:
            pass

    def seek(self, t: float) -> None:
        if self.path:
            try:
                self.player.command("seek", max(0.0, t), "absolute+exact")
            except Exception:
                pass

    def play(self, speed: float) -> None:
        if self.path:
            self.player.speed = speed
            self.player.pause = False

    def pause(self) -> None:
        if self.path:
            self.player.pause = True

    def close_player(self) -> None:
        try:
            self.gl.shutdown()
            self.player.terminate()
        except Exception:
            pass


# ── still frames from ffmpeg (no libmpv) ──────────────────────
def grab_frame(path: str, t: float, width: int = 960) -> bytes:
    """One frame at t seconds as PNG bytes (a fast keyframe seek, then exact to the frame)."""
    r = ff.run([ff.ffmpeg(), "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1", "-an", "-sn",
                "-vf", f"scale='min({width},iw)':-2", "-f", "image2pipe", "-c:v", "png", "pipe:1"], 30, binary=True)
    return r.stdout if r.returncode == 0 else b""


class StillPicture(QLabel):
    """The fallback: frames grabbed by ffmpeg in a thread; a newer request replaces an older one."""
    got = Signal(bytes, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(160, 90)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setStyleSheet("background: #000; color: #888;")
        self.path = ""
        self.pos = None
        self.paused = True
        self._want = None
        self._busy = False
        self._pix = None
        self.frames = 0
        self.got.connect(self._show)

    def load(self, path: str) -> None:
        self.path = path

    def unload(self) -> None:
        self.path = ""

    def seek(self, t: float) -> None:
        if not self.path:
            return
        self._want = t
        if not self._busy:
            self._go()

    def play(self, speed: float) -> None:
        self.paused = False

    def pause(self) -> None:
        self.paused = True

    def _go(self) -> None:
        t0, self._want = self._want, None
        if t0 is None:
            return
        self._busy = True
        path = self.path

        def work():
            png = grab_frame(path, t0) if path else b""
            self.got.emit(png, t0)
        threading.Thread(target=work, daemon=True).start()

    def _show(self, png: bytes, t0: float) -> None:
        self._busy = False
        if png:
            pm = QPixmap()
            pm.loadFromData(png, "PNG")
            self._pix = pm
            self.pos = t0
            self.frames += 1
            self._fit()
        if self._want is not None:
            self._go()

    def _fit(self) -> None:
        if self._pix is not None and not self._pix.isNull():
            self.setPixmap(self._pix.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, ev):
        self._fit()
        super().resizeEvent(ev)

    def close_player(self) -> None:
        pass


# ── the window ────────────────────────────────────────────────
class _ProjectFilm:
    """A multitrack project as the window sees a document: its film, rate, cursor; the player plays its engine.
    offset: the project frame where the film's time 0 is."""

    def __init__(self, p, engine):
        self.p, self.play_doc = p, engine
        self.film = {"fps": p.video.get("fps") or 25.0, "path": p.video.get("path", "")}
        self.rate = p.rate
        self.offset = int(p.video.get("offset") or 0)

    @property
    def cursor(self) -> int:
        return self.p.cursor


class VideoWindow(QWidget):
    closed = Signal()

    def __init__(self, win):
        super().__init__(None, Qt.Window)
        self.win = win
        self.setWindowTitle(t("vw_title"))
        self.resize(720, 420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        mpvmod = load_mpv()
        self.engine = "mpv" if mpvmod is not None else "frames"
        self.pic = None
        if mpvmod is not None:
            try:
                self.pic = MpvPicture(mpvmod, self)
            except Exception:                           # noqa: BLE001 — libmpv present but unusable
                self.engine = "frames"
        if self.pic is None:
            self.pic = StillPicture(self)
        lay.addWidget(self.pic, 1)
        bar = QWidget()
        bar.setObjectName("transport")
        h = QHBoxLayout(bar)
        h.setContentsMargins(10, 4, 10, 4)
        self.info = QLabel("")
        self.info.setObjectName("hint")
        h.addWidget(self.info, 1)
        lay.addWidget(bar)
        self.follow = Follower()
        self.film = ""
        self._last_still = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)
        geo = win.cfg.get("video_geometry") or ""
        if geo:
            try:
                self.restoreGeometry(bytes.fromhex(geo))
            except ValueError:
                pass

    def showEvent(self, ev):
        super().showEvent(ev)
        self.timer.start()
        self.tick()

    def _doc_film(self):
        pr = getattr(self.win, "proj", None)
        if pr is not None:                              # 3.0: the film a multitrack project scores
            v = pr.video.get("path") or ""
            return _ProjectFilm(pr, self.win.mt_engine(pr)), (v if v and os.path.isfile(v) else "")
        d = self.win.doc
        if d is None:
            return None, ""
        p = d.film.get("path") or ""
        return d, (p if p and os.path.isfile(p) else "")

    def tick(self) -> None:
        d, path = self._doc_film()
        if path != self.film:
            self.film = path
            if path:
                self.pic.load(path)
                self.follow = Follower(d.film.get("fps") or 25.0)
                self.setWindowTitle(f"{t('vw_title')} — {os.path.basename(path)}")
            else:
                self.pic.unload()
                self.setWindowTitle(t("vw_title"))
        if not path:
            self.info.setText(t("vw_no_film"))
            return
        pl = self.win.player
        playing = pl.state == "play" and pl.doc is getattr(d, "play_doc", d)
        audio_t = ((pl.position() if playing else d.cursor) - getattr(d, "offset", 0)) / d.rate
        if self.engine == "frames" and playing:
            now = time.monotonic()
            if now - self._last_still >= 0.25:              # a few frames a second while playing
                self._last_still = now
                self.pic.seek(audio_t)
        else:
            for act in self.follow.step(audio_t, playing, self.pic.pos, self.pic.paused):
                if act[0] == "seek":
                    self.pic.seek(act[1])
                elif act[0] == "play":
                    self.pic.play(act[1])
                else:
                    self.pic.pause()
        vt = self.pic.pos
        from .doc import fmt_time
        diff = "" if vt is None or not playing or self.engine == "frames" else \
            "  ·  " + t("vw_diff", f"{(vt - audio_t) * 1000:+.0f}")
        self.info.setText(f"{fmt_time(int(audio_t * d.rate), d.rate)}  ·  "
                          f"{t('vw_engine_' + self.engine)}{diff}")

    def release(self) -> None:
        """Let go of the film file (it is about to be replaced)."""
        self.film = ""
        self.pic.unload()

    def closeEvent(self, ev):
        self.timer.stop()
        self.win.cfg["video_geometry"] = bytes(self.saveGeometry().toHex()).decode()
        self.pic.close_player()
        self.closed.emit()
        super().closeEvent(ev)
