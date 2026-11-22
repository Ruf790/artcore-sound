"""Entry point: one window; files opened from Explorer while it runs go to the running copy.
ArtCoreSound.exe [files …]
ArtCoreSound.exe --selftest          (CI: the built exe checks itself; the result in %TEMP%)"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QEvent, QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QWidget

from . import APP_ID, APP_TITLE, VERSION, config, winint
from .i18n import set_lang

SERVER_NAME = "ArtCoreSound-" + (os.environ.get("USERNAME") or os.environ.get("USER") or "user")


def _send_to_running(paths: list[str]) -> bool:
    s = QLocalSocket()
    s.connectToServer(SERVER_NAME)
    if not s.waitForConnected(400):
        return False
    s.write(("\n".join(paths) + "\n").encode("utf-8"))
    s.waitForBytesWritten(1000)
    s.disconnectFromServer()
    return True


class TitleBars(QObject):
    """Every window and dialog gets a title bar in the colours of the theme when it is first shown."""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Show and isinstance(obj, QWidget) and obj.isWindow() and \
                not obj.property("acTitled"):
            obj.setProperty("acTitled", True)
            from . import style
            winint.dark_title_bar(int(obj.winId()), style.IS_DARK)
        return False


def selftest() -> int:
    """CI: the built exe finds ffmpeg / ffprobe and PortAudio, opens a 5.1 AC-3 file made by ffmpeg, edits it
    (cut, paste, silence, trim, undo / redo, a fade, normalize, a rate change), saves it in every format and reads
    the results back, builds the window and plays a second into the null output."""
    import tempfile
    import time
    import traceback
    out = os.path.join(tempfile.gettempdir(), "artcore_sound_selftest.txt")
    lines: list[str] = []
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        tmp = os.path.join(tempfile.gettempdir(), "artcore_sound_selftest")
        os.makedirs(tmp, exist_ok=True)
        os.environ.setdefault("ARTCORE_SOUND_DATA", os.path.join(tmp, "data"))
        app = QApplication(sys.argv)
        from . import ff, media, proc, store, style
        style.set_theme("dark")
        app.setStyle("Fusion")
        app.setStyleSheet(style.stylesheet())
        lines.append(f"OK {VERSION}; ffmpeg {ff.version()} at {ff.ffmpeg()}; ffprobe {ff.ffprobe()}")
        if not ff.ffmpeg() or not ff.ffprobe():
            raise RuntimeError("ffmpeg / ffprobe not found")
        enc = ff.caps()["encoders"]
        need = {"pcm_s16le", "pcm_s24le", "pcm_f32le", "flac", "libmp3lame", "aac", "libopus", "ac3"}
        if need - enc:
            raise RuntimeError(f"this ffmpeg lacks {sorted(need - enc)}")
        lines.append(f"soxr: {'yes' if proc._has_soxr() else 'no'}")
        try:
            import sounddevice as sd
            apis = [h["name"] for h in sd.query_hostapis()]
            lines.append(f"PortAudio {sd.get_portaudio_version()[1]}; host APIs {apis}; "
                         f"{len(sd.query_devices())} devices")
            if sys.platform == "win32" and not any("WASAPI" in a for a in apis):
                raise RuntimeError("PortAudio without WASAPI")
        except OSError as e:
            raise RuntimeError(f"PortAudio: {e}")
        src = os.path.join(tmp, "s51.ac3")
        r = ff.run([ff.ffmpeg(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=4:r=48000",
                    "-filter_complex", "[0]pan=5.1|c0=c0|c1=0.5*c0|c2=c0|c3=c0|c4=c0|c5=c0", "-c:a", "ac3", src], 60)
        if r.returncode != 0:
            raise RuntimeError("making the test file failed: " + r.stderr[-300:])
        session = store.Session(os.path.join(tmp, "work"))
        d = media.open_file(src, session.folder())
        lines.append(f"opened: {d.frames} frames, {d.rate} Hz, {d.channels} ch {d.labels}, peak {d.peak().max():.3f}")
        if d.channels != 6 or d.rate != 48000 or abs(d.frames - 4 * 48000) > 2048:
            raise RuntimeError("the 5.1 file did not open right")
        n0 = d.frames
        clip = d.cut(48000, 96000)
        d.paste(clip, 0)
        d.silence(0, 4800)
        d.trim(0, 3 * 48000)
        d.undo()
        d.redo()
        if d.frames != 3 * 48000 or len(d.history) != 5:
            raise RuntimeError(f"edits: {d.frames} frames, {len(d.history)} steps")
        s = proc.apply_op(d, "fade_in", 0, 24000)
        d.replace(0, 24000, s, "fade in")
        s = proc.apply_op(d, "normalize", 0, d.frames, db=-1.0)
        d.replace(0, d.frames, s, "normalize")
        pk = float(d.peak().max())
        if abs(pk - proc.db_to_gain(-1.0)) > 1e-3:
            raise RuntimeError(f"normalize: peak {pk}")
        lines.append(f"edited: {n0} → {d.frames} frames, {len(d.history)} undo steps, peak {pk:.4f}")
        for fmt in media.FORMATS:
            opt = media.default_option(fmt, d.channels)
            path = os.path.join(tmp, f"out_{fmt}{media.FORMATS[fmt].ext}")
            media.save(d, path, fmt, opt)
            info = ff.probe(path)
            tr = info.tracks[0]
            lines.append(f"  {fmt} {opt}: {tr.codec} {tr.rate} Hz {tr.channels} ch {info.duration:.2f} s")
            ch, _rate = media.out_shape(fmt, d.channels, d.rate)
            if tr.channels != ch or abs(info.duration - 3.0) > 0.1:
                raise RuntimeError(f"{fmt}: {tr.channels} ch, {info.duration:.2f} s")
        s = proc.convert_document(d, 44100, 2, "stereo")
        lines.append(f"to 44.1 kHz stereo: {s.frames} frames")
        if abs(s.frames - 3 * 44100) > 64 or s.channels != 2:
            raise RuntimeError("the rate / channel change failed")
        cfg = config.load()
        cfg.update(check_updates=False)
        os.environ["ARTCORE_AUDIO"] = "null"
        from .window import MainWindow
        win = MainWindow(cfg, session)
        win.show()
        win.add_doc(media.open_file(src, session.folder()))
        win.play()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.0:
            app.processEvents()
            time.sleep(0.02)
        pos = win.player.position()
        win.stop_play()
        lines.append(f"window: {len(win.docs)} document, played to {pos} ({win.player.backend})")
        if not 0.5 * 48000 < pos < 1.6 * 48000:
            raise RuntimeError(f"playback position {pos}")
        for x in win.docs:
            x.mark_saved()
        win.close()
        session.close()
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return 0
    except Exception:
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\nFAIL\n" + traceback.format_exc())
        return 1


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    winint.init_process(APP_ID)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationVersion(VERSION)
    if _send_to_running([os.path.abspath(p) for p in paths]):
        return 0
    cfg = config.load()
    set_lang(cfg["language"])
    from . import style, store
    style.set_look(cfg.get("look", "classic"))
    style.set_theme(cfg.get("theme", "dark"))
    app.setStyle("Fusion")
    app.setStyleSheet(style.stylesheet())
    app.setWindowIcon(style.app_icon())
    tb = TitleBars(app)
    app.installEventFilter(tb)
    session = store.Session(config.work_root(cfg))
    from .window import MainWindow
    win = MainWindow(cfg, session)
    server = QLocalServer()
    QLocalServer.removeServer(SERVER_NAME)
    server.listen(SERVER_NAME)

    def on_conn():
        sock = server.nextPendingConnection()

        def read():
            data = bytes(sock.readAll()).decode("utf-8", "replace")
            got = [ln for ln in data.splitlines() if ln.strip()]
            if got:
                win.open_paths(got)
            win.showNormal()
            win.raise_()
            win.activateWindow()
        sock.readyRead.connect(read)
    server.newConnection.connect(on_conn)
    win.show()
    if paths:
        win.open_paths([os.path.abspath(p) for p in paths])
    from PySide6.QtCore import QTimer
    QTimer.singleShot(3000, win.maybe_check_updates)
    rc = app.exec()
    server.close()
    session.close()
    return rc
