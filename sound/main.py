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
        et = ev.type()
        if et == QEvent.Show and isinstance(obj, QWidget) and obj.isWindow() and \
                not obj.property("acTitled"):
            obj.setProperty("acTitled", True)
            from . import style, winstate
            winint.dark_title_bar(int(obj.winId()), style.IS_DARK)
            winstate.restore(obj)                       # the size and place it had last time (1.0)
        elif et == QEvent.Hide and isinstance(obj, QWidget) and obj.isWindow() and obj.property("acTitled"):
            from . import winstate
            winstate.save(obj)
        return False


def selftest() -> int:
    """CI: the built exe finds ffmpeg / ffprobe and PortAudio, opens a 5.1 AC-3 file made by ffmpeg, edits it
    (cut, paste, silence, trim, undo / redo, a fade, normalize, a rate change), saves it in every format and reads
    the results back, measures the loudness (SciPy) against ffmpeg's ebur128, normalizes to −23 LUFS, removes the
    DC offset and trims the silence, builds the window and plays a second into the null output. 0.3: the filters the
    effects need (rubberband, atempo, asetrate, aresample), every effect on the 5.1 file checked by the numbers (EQ
    gain, the limiter's true-peak ceiling, the compressor, the delay's tail, the reverb, noise reduction, a click found
    and filled, tempo by its length, pitch by FFT) and a live preview through the null output. 0.4: the spectrum of
    the 440 Hz tone, a spectrogram tile, markers and regions written into a WAV (cue / labl) and read back and kept
    by an edit, a recording through the null input, and a WASAPI input stream opened (or a clear word that there is
    no input device). 1.0: a batch of 3 files in worker processes of the exe, a long Cyrillic path, the crash
    journal, the error report. 2.0: the listening volume (−12 dB heard, the meters unchanged, the file untouched)."""
    import tempfile
    import time
    import traceback

    import numpy as np
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
        from . import analysis
        import re
        wav = os.path.join(tmp, "out_wav.wav")
        st = analysis.analyze(d)
        r = ff.run([ff.ffmpeg(), "-hide_banner", "-nostats", "-i", wav, "-af", "ebur128=peak=true", "-f", "null", "-"],
                   120)
        summ = r.stderr[r.stderr.rfind("Summary"):]
        ref = float(re.search(r"I:\s*(-?[\d.]+) LUFS", summ).group(1))
        lines.append(f"loudness: {st.integrated:.2f} LUFS (ffmpeg {ref:.1f}), LRA {st.lra:.1f} LU, "
                     f"true peak {analysis.to_db(st.true_peak):.2f} dBTP, RMS {analysis.to_db(st.rms):.2f} dBFS")
        if abs(st.integrated - ref) > 0.15:
            raise RuntimeError("the loudness does not match ffmpeg's")
        s, res = proc.normalize(d, 0, d.frames, "loudness", -23.0, -1.0)
        d.replace(0, d.frames, s, "normalize lufs")
        s, _dc = proc.remove_dc(d, 0, d.frames)
        d.replace(0, d.frames, s, "dc")
        lufs = analysis.analyze(d).integrated
        d.silence(0, 24000)
        edges = proc.sound_edges(d, 0, d.frames, -60.0)
        d.keep(0, d.frames, *edges)
        lines.append(f"normalized: {lufs:.2f} LUFS (limited: {res.limited}), trimmed to {d.frames} frames")
        if abs(lufs + 23.0) > 0.1 or d.frames > 3 * 48000 - 23000:
            raise RuntimeError("normalize / trim silence")
        _selftest_effects(d, lines)
        _selftest_04(src, session, tmp, lines)
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
        from . import fx
        wd = win.doc
        pk0 = 20 * np.log10(float(wd.peak().max()))
        chain = fx.PreviewChain(fx.get("limit"), wd.rate, wd.labels, {"gain": 12.0, "ceiling": -12.0,
                                                                     "true_peak": False})
        win.player.play(wd, 0, wd.frames, loop=True, fx=chain)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.8:
            app.processEvents()
            time.sleep(0.02)
        lv = win.player.levels()
        top = 20 * np.log10(max(1e-9, float(np.max(lv)))) if lv is not None else -200.0
        chain.set_bypass(True)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.5:
            app.processEvents()
            time.sleep(0.02)
        lv2 = win.player.levels()
        top2 = 20 * np.log10(max(1e-9, float(np.max(lv2)))) if lv2 is not None else -200.0
        win.stop_play()
        lines.append(f"preview: limiter +12 dB, ceiling −12 dB → meters {top:.2f} dBFS; bypass {top2:.2f} dBFS "
                     f"(the file {pk0:.2f}); {chain.blocks} blocks")
        if abs(top + 12.0) > 0.3 or abs(top2 - pk0) > 0.5 or chain.blocks < 5:
            raise RuntimeError("the live preview")
        for x in win.docs:
            x.mark_saved()
        win.close()
        _selftest_10(src, session, tmp, lines)
        _selftest_20(src, session, tmp, lines)
        session.close()
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return 0
    except Exception:
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\nFAIL\n" + traceback.format_exc())
        return 1


def _selftest_effects(d, lines: list) -> None:
    """The effects (0.3) on the opened 5.1 document, checked by the numbers."""
    import numpy as np
    from . import analysis, ff, fx
    from .fx import tempo as tm
    flt = ff.caps()["filters"]
    need = {"atempo", "asetrate", "aresample", "rubberband"}
    lines.append("filters: " + ", ".join(f"{f} {'yes' if f in flt else 'NO'}" for f in sorted(need)))
    if need - flt:
        raise RuntimeError(f"this ffmpeg lacks the filters {sorted(need - flt)}")
    n, rate = d.frames, d.rate
    mid = slice(n // 3, 2 * n // 3)
    x = d.read(0, n)
    src = fx.render(d, 0, n, fx.get("eq"), {"b4_gain": 6.0, "b4_freq": 440.0, "b4_q": 0.7})
    y = src.read(0, n)
    g = 20 * np.log10(np.abs(y[mid, 0]).max() / np.abs(x[mid, 0]).max())
    lines.append(f"  EQ +6 dB at 440 Hz: {g:+.2f} dB")
    if abs(g - 6.0) > 0.1:
        raise RuntimeError("EQ")
    src = fx.render(d, 0, n, fx.get("limit"), {"gain": 24.0, "ceiling": -6.0, "true_peak": True})
    tp = analysis.to_db(_tp_of(src, rate, d.labels))
    lines.append(f"  limiter +24 dB, ceiling −6 dBTP: true peak {tp:.2f} dBTP")
    if not -6.5 < tp <= -5.9:
        raise RuntimeError("limiter")
    src = fx.render(d, 0, n, fx.get("comp"), {"threshold": -30.0, "ratio": 4.0, "knee": 0.0, "attack": 1.0})
    lvl = 20 * np.log10(np.abs(x[mid, 0]).max())
    out = 20 * np.log10(np.abs(src.read(0, n)[mid, 0]).max())
    want = lvl - max(0.0, (lvl + 30.0) * 0.75)
    lines.append(f"  compressor −30 dB 4:1: {lvl:.2f} → {out:.2f} dBFS (curve {want:.2f})")
    if abs(out - want) > 0.3:
        raise RuntimeError("compressor")
    a = n - rate // 2
    src = fx.render(d, a, n, fx.get("delay"), {"time": 300.0, "feedback": 30.0}, keep_tail=True)
    src2 = fx.render(d, a, n, fx.get("reverb"), {"decay": 1.0}, keep_tail=True)
    tails = ((src.frames - rate // 2) / rate, (src2.frames - rate // 2) / rate)
    lines.append(f"  delay / reverb tails: {tails[0]:.2f} s / {tails[1]:.2f} s")
    if src.frames < rate // 2 + 0.6 * rate or src2.frames < rate // 2 + 0.8 * rate:
        raise RuntimeError("tails")
    rng = np.random.default_rng(1)
    fx.get("nr").profile = fx.noise_profile([rng.standard_normal((rate, d.channels)) * 0.01], rate, d.channels)
    src = fx.render(d, 0, n, fx.get("nr"), {"reduction": 20.0})
    y = src.read(0, n)
    keep = 20 * np.log10(np.abs(y[mid, 0]).max() / np.abs(x[mid, 0]).max())
    lines.append(f"  noise reduction: the tone {keep:+.2f} dB")
    if abs(keep) > 0.5 or not np.isfinite(y).all():
        raise RuntimeError("noise reduction")
    from .doc import Document
    from . import store
    xc = x.copy()
    xc[rate, 0] += 0.5
    dc = Document(rate=rate, channels=d.channels, folder=d.folder, labels=d.labels, layout=d.layout)
    dc.set_initial(store.from_array(d.folder, xc))
    marks = fx.scan_clicks(dc, 0, n, {"sensitivity": 50.0})
    src = fx.render(dc, 0, n, fx.get("declick"), {})
    err = float(np.abs(src.read(rate - 4, 9)[:, 0] - x[rate - 4:rate + 5, 0]).max())
    lines.append(f"  click removal: found {len(marks)} at {marks[:3]}, filled to {err:.4f}")
    if not any(abs(m - rate) <= 2 for m in marks) or err > 0.05:
        raise RuntimeError("click removal")
    t = fx.get("tempo")
    src = t.render(d, 0, n, {"tempo": 25.0})
    p = t.render(d, 0, n, {"semitones": 12})
    yp = p.read(0, p.frames)[p.frames // 4:3 * p.frames // 4, 0]
    sp = np.abs(np.fft.rfft(yp * np.hanning(len(yp))))
    f = np.argmax(sp) * rate / len(yp)
    lines.append(f"  tempo +25 %: {n} → {src.frames} frames; pitch +12: 440 → {f:.1f} Hz ({tm.engine({})})")
    if abs(src.frames - n / 1.25) > 0.01 * n or abs(f - 880) > 4:
        raise RuntimeError("tempo / pitch")


def _selftest_04(path: str, session, tmp: str, lines: list) -> None:
    """Spectrum, spectrogram, markers in a WAV, recording (0.4)."""
    import time

    import numpy as np
    from . import marks, media, record, spectrogram, spectrum
    d = media.open_file(path, session.folder())
    st = spectrum.Settings(fft=8192, window="flattop")
    res = spectrum.spectrum(d, 0, d.frames, st)
    f, v = spectrum.peak(res.freqs, res.levels()[0], win="flattop")
    ref = 20 * np.log10(float(np.abs(d.read(d.rate, d.rate)[:, 0]).max()))
    lines.append(f"spectrum: peak {f:.1f} Hz {v:.2f} dB (sample peak {ref:.2f} dBFS), {spectrum.note(f)}, "
                 f"{res.segments} segments")
    if abs(f - 440) > 1 or abs(v - ref) > 0.3:
        raise RuntimeError("spectrum")
    sst = spectrogram.Settings(fft=2048, scale="lin")
    order, jobs = spectrogram.jobs_for_view(d, 0, d.frames / 1000, 1000, 400, sst)
    j = jobs[order[0]]
    t0 = time.monotonic()
    arr = spectrogram.compute_tile(j.reader, j.frames, j.rate, j.spp, j.tile, j.rows, j.fft, j.scale, j.chs)
    row = int(np.argmax(arr[0][:, 100]))
    fr = float(spectrogram.f_of_frac(1 - (row + 0.5) / 400, d.rate, "lin"))
    lines.append(f"spectrogram tile {arr.shape} in {(time.monotonic() - t0) * 1000:.0f} ms: the line at {fr:.0f} Hz")
    if abs(fr - 440) > 40:
        raise RuntimeError("spectrogram")
    d.add_marker(48000, "Метка 1")
    d.add_region(72000, 120000, "Регион «1»")
    wav = os.path.join(tmp, "markers.wav")
    media.save(d, wav, "wav", "24")
    back = marks.read_wav(wav)
    d.delete(0, 24000)
    lines.append(f"markers in WAV: {back}; after deleting 0.5 s: {[(m.pos, m.end) for m in d.markers]}")
    if back != [("Метка 1", 48000, None), ("Регион «1»", 72000, 120000)] or \
            [(m.pos, m.end) for m in d.markers] != [(24000, None), (48000, 96000)]:
        raise RuntimeError("markers")
    lines.append("input: " + record.probe_input())
    r = record.Recorder()
    if not r.open({"null": True, "name": "null", "channels": 2, "rate": 48000}, 2, 48000):
        raise RuntimeError("the null input: " + r.error)
    r.record(session.folder())
    time.sleep(0.6)
    src = r.stop()
    r.close()
    x = src.read(0, src.frames) if src is not None else np.zeros((0, 2))
    lines.append(f"recorded (null input): {len(x) / 48000:.2f} s, peak {float(np.abs(x).max(initial=0)):.3f}, "
                 f"{r.overflows} dropped")
    if not 0.4 < len(x) / 48000 < 0.9 or abs(float(np.abs(x).max(initial=0)) - 0.25) > 0.01 or r.overflows:
        raise RuntimeError("recording")


def _selftest_10(src: str, session, tmp: str, lines: list) -> None:
    """1.0: a batch of 3 files in 2 worker processes of this exe (normalize to −16 LUFS + limiter → FLAC, a
    subfolder kept, a broken file reported), a path longer than 260 characters with Cyrillic names, the crash
    journal written and read back, and the error report."""
    import shutil
    import time

    from . import analysis, batch, crash, ff, media, recovery
    root = os.path.join(tmp, "batch")
    shutil.rmtree(root, ignore_errors=True)
    inp = os.path.join(root, "Вход", "Подпапка")
    os.makedirs(inp)
    for i, (f, ch) in enumerate(((330, 2), (550, 1), (770, 6))):
        p = os.path.join(inp if i else os.path.dirname(inp), f"Тон {i + 1}.wav")
        r = ff.run([ff.ffmpeg(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=f={f}:d=3:r=48000", "-ac", str(ch), "-c:a", "pcm_s16le", p], 60)
        if r.returncode != 0:
            raise RuntimeError("making the batch files failed")
    with open(os.path.join(inp, "битый.wav"), "wb") as f:
        f.write(b"RIFF....WAVEnot a wave" * 20)
    out = os.path.join(root, "Выход")
    job = batch.Job(batch.Source("folder", os.path.join(root, "Вход"), True),
                    [batch.Step("normalize", {"mode": "loudness", "target": -16.0, "ceiling": -1.0}),
                     batch.Step("limit", {"ceiling": -1.0})],
                    batch.Output("flac", "24", "other", out, True, "{n:2} {name}", ""), workers=2)
    t0 = time.monotonic()
    r = batch.Runner(job, os.path.join(tmp, "work"))
    r.start()
    if not r.wait(300):
        r.cancel()
        raise RuntimeError("the batch did not finish")
    st = {x.status for x in r.tasks}
    oks = [x for x in r.tasks if x.status == "ok"]
    lines.append(f"batch: {len(r.tasks)} tasks in {time.monotonic() - t0:.1f} s, 2 worker processes: "
                 + ", ".join(f"{os.path.basename(x.src)} {x.status}"
                             + (f" {x.after.get('lufs')} LUFS" if x.status == "ok" else f" ({x.error})")
                             for x in r.tasks))
    if len(oks) != 3 or "error" not in st or not os.path.isfile(os.path.join(out, "Подпапка", "04 Тон 3.flac")):
        raise RuntimeError("the batch")
    for x in oks:
        sess = store_session(os.path.join(tmp, "check"))
        d = media.open_file(x.out, sess.folder())
        lufs = analysis.analyze(d).integrated
        sess.close()
        if abs(lufs + 16.0) > 0.3:
            raise RuntimeError(f"batch: {x.out} at {lufs:.2f} LUFS")
    deep = tmp
    while len(deep) < 280:
        deep = os.path.join(deep, "Очень длинное имя папки с кириллицей")
    os.makedirs(deep, exist_ok=True)
    lp = os.path.join(deep, "Фильм — дорожка 5.1.ac3")
    shutil.copy(src, lp)
    d = media.open_file(lp, session.folder())
    media.save(d, os.path.join(deep, "Копия.flac"), "flac", "24")
    ok_long = os.path.isfile(os.path.join(deep, "Копия.flac"))
    lines.append(f"a path of {len(lp)} characters: opened {d.frames} frames, saved: {ok_long}")
    if not ok_long:
        raise RuntimeError("the long path")
    d.delete(0, 4800)
    d.add_marker(9600, "Метка")
    recovery.write(d, session.path)
    e = recovery.Entry(session.path, d.folder, recovery.to_dict(d, session.path))
    sess2 = store_session(os.path.join(tmp, "work2"))
    back = recovery.restore(e, sess2)
    same = back.frames == d.frames and len(back.markers) == 1 and back.can_undo()
    lines.append(f"recovery journal: {back.frames} frames, {len(back.history)} steps, markers {len(back.markers)}")
    sess2._adopted = []                                   # the session folder still belongs to this run
    sess2.close()
    if not same:
        raise RuntimeError("the recovery journal")
    try:
        raise ValueError("a test error")
    except ValueError:
        path = crash.write_log(crash.report(*sys.exc_info()))
    lines.append(f"error report: {os.path.basename(path)}")
    if not path or "ValueError: a test error" not in open(path, encoding="utf-8").read():
        raise RuntimeError("the error report")


def store_session(path: str):
    from . import store
    return store.Session(path)


def _tp_of(src, rate: int, labels) -> float:
    from . import analysis
    an = analysis.Analyzer(rate, labels, loudness=False, true_peak=True)
    pos = 0
    while pos < src.frames:
        k = min(65536, src.frames - pos)
        an.feed(src.read(pos, k))
        pos += k
    return an.result().true_peak


def _selftest_20(src, session, tmp: str, lines: list) -> None:
    """2.0: the listening volume — the null output 12 dB quieter, the meters still the file's level, the file not
    touched; then the input monitor of the Record window at the same volume."""
    import time

    import numpy as np

    from . import media, record
    from .player import InputMonitor, Player, Volume
    src = media.open_file(src, session.folder()) if isinstance(src, str) else src

    def capture(vol):
        blocks = []
        p = Player(volume=vol)
        p.sink = blocks.append
        if not p.play(src, 0, src.frames):
            raise RuntimeError("the null playback")
        time.sleep(0.5)
        lv = p.levels().copy()
        p.stop()
        y = np.concatenate(blocks[2:-1])
        return float(np.sqrt(np.mean(np.square(y, dtype=np.float64)))), lv

    before = src.read(0, 65536).copy()
    r0, lv0 = capture(Volume(0))
    r1, lv1 = capture(Volume(-12))
    db = 20 * np.log10(max(1e-12, r1) / max(1e-12, r0))
    same = float(np.max(np.abs(lv0 - lv1)))
    lines.append(f"listening volume: −12 dB set → {db:.2f} dB heard; meters differ by {same:.4f}")
    if abs(db + 12) > 0.2 or same > 0.02 or not np.array_equal(src.read(0, 65536), before):
        raise RuntimeError("the listening volume")
    r = record.Recorder()
    devs = [d for d in record.input_devices() if d.get("null")] or [{"null": True, "name": "null"}]
    blocks = []
    m = InputMonitor(volume=Volume(-6))
    m.sink = blocks.append
    if not r.open(devs[0], 2, 48000) or not m.open(48000, 2):
        raise RuntimeError("the input monitor")
    r.monitor = m
    time.sleep(0.4)
    r.monitor = None
    m.close()
    r.close()
    pk = float(np.max(np.abs(np.concatenate(blocks[4:-1])[:, 0]))) if len(blocks) > 6 else 0.0
    mdb = 20 * np.log10(max(1e-9, pk) / 0.25)
    lines.append(f"input monitor: −6 dB set → {mdb:.2f} dB heard")
    if abs(mdb + 6) > 0.3:
        raise RuntimeError("the input monitor's volume")


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
    from . import crash, recovery, winstate
    winstate.cfg = cfg
    crash.install()
    root = config.work_root(cfg)
    lost = recovery.scan(root)                    # unsaved documents of a run that crashed (before they are removed)
    session = store.Session(root, clean=not lost)
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
    if lost:
        QTimer.singleShot(200, lambda: win.offer_recovery(lost))
    QTimer.singleShot(3000, win.maybe_check_updates)
    rc = app.exec()
    server.close()
    session.close()
    return rc
