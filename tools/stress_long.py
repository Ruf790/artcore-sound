"""The long-film check (1.0): ffmpeg makes a film of ~2.5 h in 5.1 at 48 kHz — an MKV with a small video, an AC-3
track, an E-AC-3 track, chapters, and a DTS track when asked — and ArtCore Sound is timed on it the way the editor
uses it: opening (decoding + peaks), the memory of the process, the disk of the working files, drawing the waveform
and the spectrogram while scrolling and zooming, cut / paste / undo over the whole file, the loudness analysis of all
of it, a LUFS normalization, the limiter and one heavy effect over all of it, saving to FLAC and AC-3, markers,
closing and the clean-up. The result is a table (report.md) and the figures (report.json) in the report folder.

    python tools/stress_long.py <report folder> [--minutes 150] [--dts] [--heavy nr|reverb|declick|eq] [--keep]

The film goes to <report folder>/film (made once, reused by later runs); the working files to the system's temp
folder (ARTCORE_SOUND_TEMP changes that)."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ARTCORE_AUDIO", "null")


# ── memory ────────────────────────────────────────────────────
def rss() -> int:
    """The resident memory of this process, bytes."""
    try:
        import psutil
        return psutil.Process().memory_info().rss
    except ImportError:
        pass
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return c.WorkingSetSize
    try:
        with open("/proc/self/status") as f:
            for ln in f:
                if ln.startswith("VmRSS:"):
                    return int(ln.split()[1]) * 1024
    except OSError:
        pass
    return 0


def private_bytes() -> int:
    """Memory the process owns — without the pages of the mapped sound files, which the system drops whenever it
    needs the memory (they are the file cache, not the program's memory). Windows: the private bytes; Linux: the
    anonymous memory."""
    if sys.platform == "win32":
        try:
            import psutil
            return psutil.Process().memory_full_info().private
        except Exception:                                    # noqa: BLE001
            return rss()
    try:
        with open("/proc/self/smaps_rollup") as f:
            for ln in f:
                if ln.startswith("Anonymous:"):
                    return int(ln.split()[1]) * 1024
    except OSError:
        pass
    return rss()


class MemWatch:
    """Samples the memory every 50 ms; peak() of the current stage."""

    def __init__(self):
        self._peak = self._ppeak = 0
        self._run = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self._run:
            self._peak = max(self._peak, rss())
            self._ppeak = max(self._ppeak, private_bytes())
            time.sleep(0.05)

    def reset(self):
        self._peak, self._ppeak = rss(), private_bytes()

    def peak(self) -> tuple[int, int]:
        return max(self._peak, rss()), max(self._ppeak, private_bytes())

    def stop(self):
        self._run = False


def mb(n: int) -> str:
    return f"{n / 1048576:,.0f} MB".replace(",", " ")


def gb(n: int) -> str:
    return f"{n / 1073741824:.2f} GB"


def folder_bytes(path: str) -> int:
    total = 0
    for dp, _dn, fs in os.walk(path):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


# ── the film ──────────────────────────────────────────────────
def make_film(folder: str, minutes: float, dts: bool) -> str:
    """An MKV: 320×180 video at 2 fps, 5.1 AC-3 640k «rus» (default) + E-AC-3 384k «eng» (+ DTS 1509k), a chapter
    every 10 minutes. The sound: a tone per channel with pink noise under it and a few loud bursts — something
    LUFS, the limiter and the spectrogram have to work on."""
    from sound import ff
    os.makedirs(folder, exist_ok=True)
    name = f"film_{int(minutes)}min{'_dts' if dts else ''}.mkv"
    path = os.path.join(folder, name)
    if os.path.isfile(path) and os.path.getsize(path) > 1 << 20:
        return path
    exe = ff.ffmpeg()
    sec = minutes * 60
    meta = os.path.join(folder, "chapters.txt")
    with open(meta, "w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\ntitle=ArtCore stress film\n")
        k = 0
        while k * 600 < sec:
            f.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={k * 600000}\nEND={int(min(sec, (k + 1) * 600) * 1000)}\n"
                    f"title=Глава {k + 1}\n")
            k += 1
    tones = "|".join(f"0.25*sin(2*PI*{f}*t)*(0.6+0.4*sin(2*PI*t/{p}))"
                     for f, p in ((220, 7), (330, 11), (440, 13), (55, 17), (660, 19), (880, 23)))
    graph = (f"aevalsrc='{tones}':s=48000:d={sec}:c=5.1[t];"
             f"anoisesrc=d={sec}:c=pink:r=48000:a=0.03,pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0[n];"
             "[t][n]amix=2:normalize=0,volume='if(lt(mod(t,300),2),2.5,1)':eval=frame,"
             + ("asplit=3[a0][a1][a2]" if dts else "asplit=2[a0][a1]"))
    args = [exe, "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x203040:s=320x180:r=2:d={sec}",
            "-i", meta, "-filter_complex", graph, "-map", "0:v", "-map", "[a0]", "-map", "[a1]"]
    if dts:
        args += ["-map", "[a2]"]
    args += ["-map_metadata", "1", "-map_chapters", "1", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "40",
             "-c:a:0", "ac3", "-b:a:0", "640k", "-c:a:1", "eac3", "-b:a:1", "384k"]
    if dts:
        args += ["-c:a:2", "dca", "-strict", "-2", "-b:a:2", "1509k", "-metadata:s:a:2", "language=eng"]
    args += ["-metadata:s:a:0", "language=rus", "-metadata:s:a:0", "title=Дубляж 5.1",
             "-metadata:s:a:1", "language=eng", "-disposition:a:0", "default", path + ".part.mkv"]
    t0 = time.perf_counter()
    subprocess.run(args, check=True)
    os.replace(path + ".part.mkv", path)
    print(f"film made in {time.perf_counter() - t0:.0f} s: {path} ({gb(os.path.getsize(path))})", flush=True)
    return path


# ── the run ───────────────────────────────────────────────────
class Report:
    def __init__(self):
        self.rows: list = []
        self.data: dict = {}

    def add(self, group: str, what: str, value: str, key: str = "", num=None) -> None:
        self.rows.append((group, what, value))
        print(f"  {group:<12} {what:<46} {value}", flush=True)
        if key:
            self.data[key] = num if num is not None else value

    def markdown(self, head: list[str]) -> str:
        out = head + ["", "| | | |", "|---|---|---|"]
        last = None
        for g, w, v in self.rows:
            out.append(f"| {'**' + g + '**' if g != last else ''} | {w} | {v} |")
            last = g
        return "\n".join(out) + "\n"


def timed(fn, *a, **kw):
    t0 = time.perf_counter()
    r = fn(*a, **kw)
    return r, time.perf_counter() - t0


def run(out: str, minutes: float, dts: bool, heavy: str, keep: bool) -> int:
    from PySide6.QtWidgets import QApplication
    from sound import VERSION, analysis, config, ff, fx, media, proc, store, style
    from sound.i18n import set_lang
    os.makedirs(out, exist_ok=True)
    film = make_film(os.path.join(out, "film"), minutes, dts)
    set_lang("en")
    app = QApplication.instance() or QApplication(sys.argv)
    style.set_theme("dark")
    app.setStyle("Fusion")
    app.setStyleSheet(style.stylesheet())
    rep = Report()
    mem = MemWatch()
    info = ff.probe(film)
    root = config.work_root({})
    sess = store.Session(root)
    free0 = shutil.disk_usage(sess.path).free
    head = [f"# ArtCore Sound {VERSION} — the long-film check", "",
            f"- {platform.platform()}, Python {platform.python_version()}, {os.cpu_count()} cores; "
            f"ffmpeg {ff.version()}",
            f"- the film: {os.path.basename(film)}, {info.duration / 3600:.2f} h, {gb(info.size)}, "
            + ", ".join(f"{ff.codec_name(t)} {t.channels} ch {t.lang}" for t in info.tracks)
            + f", {len(info.chapters)} chapters",
            f"- the working files: {sess.path} (free {gb(free0)})"]
    print("\n".join(head), flush=True)
    base_rss = rss()
    rep.add("process", "memory at the start (RSS)", mb(base_rss), "rss_start", base_rss)
    results = {}

    # opening every track
    tracks = info.tracks
    d = None
    for tr in tracks:
        mem.reset()
        folder = sess.folder()
        dd, sec = timed(media.open_file, film, folder, tr.pos, None, info)
        pk, ppk = mem.peak()
        speed = dd.duration() / sec
        rep.add("open", f"{ff.codec_name(tr)} {tr.channels} ch: decode + peaks", f"{sec:.1f} s ({speed:.0f}× real time)",
                f"open_{tr.codec}_s", round(sec, 2))
        rep.add("open", f"{ff.codec_name(tr)}: peak memory RSS / private", f"{mb(pk)} / {mb(ppk)}",
                f"open_{tr.codec}_rss", pk)
        if d is None:
            d = dd
            results["chapters"] = len(d.markers)
        else:
            dd.close()
    disk = folder_bytes(d.folder)
    rep.add("open", "working files of one track (sound + peaks)", f"{gb(disk)} ({disk / max(1, info.size):.1f}× the film)",
            "work_bytes", disk)
    rep.add("open", "chapters → regions", str(results["chapters"]), "regions", results["chapters"])

    # drawing
    from sound.waveview import WaveView
    v = WaveView()
    v.resize(1600, 720)
    v.show()
    v.set_doc(d)
    app.processEvents()
    n = d.frames
    w = v.lane_width()
    for label, spp in (("the whole film", n / w), ("10 minutes on screen", 600 * d.rate / w),
                       ("10 seconds on screen", 10 * d.rate / w), ("samples (2 per pixel)", 2.0)):
        mem.reset()
        k, t0 = 0, time.perf_counter()
        span = spp * w
        while time.perf_counter() - t0 < 3.0:
            start = (k * 0.137 % 1.0) * max(0.0, n - span)
            v.set_view(start, spp)
            v.grab()
            k += 1
        fps = k / (time.perf_counter() - t0)
        rep.add("waveform", f"scroll, {label}", f"{fps:.0f} frames/s", f"wave_fps_{int(spp)}", round(fps, 1))
    mem.reset()
    k, t0 = 0, time.perf_counter()
    spp = n / w
    while time.perf_counter() - t0 < 3.0:
        spp = spp * 0.5 if spp > 4 else n / w
        v.set_view(max(0.0, n / 2 - spp * w / 2), spp)
        v.grab()
        k += 1
    rep.add("waveform", "zooming in and out about the middle", f"{k / (time.perf_counter() - t0):.0f} frames/s",
            "wave_zoom_fps", round(k / (time.perf_counter() - t0), 1))
    rep.add("waveform", "peak memory while drawing", mb(mem.peak()[0]), "wave_rss", mem.peak()[0])
    v.set_mode("spec")
    for label, spp in (("the whole film", n / w), ("10 minutes", 600 * d.rate / w), ("10 seconds", 10 * d.rate / w)):
        mem.reset()
        lat = []
        for i in range(3):
            start = (0.1 + 0.3 * i) * max(0.0, n - spp * w)
            v.set_view(start, spp)
            t0 = time.perf_counter()
            v.grab()
            while v.sg_pending() and time.perf_counter() - t0 < 120:
                app.processEvents()
                time.sleep(0.005)
            v.grab()
            lat.append(time.perf_counter() - t0)
        k, t0 = 0, time.perf_counter()
        while time.perf_counter() - t0 < 2.0:
            v.grab()
            k += 1
        rep.add("spectrogram", f"{label}: a new view fully drawn", f"{sum(lat) / len(lat) * 1000:.0f} ms "
                f"(then {k / (time.perf_counter() - t0):.0f} frames/s)", f"sg_ms_{int(spp)}",
                round(sum(lat) / len(lat) * 1000))
    rep.add("spectrogram", "peak memory", mb(mem.peak()[0]), "sg_rss", mem.peak()[0])
    v.set_mode("wave")
    v.close_engine()

    # editing the whole film
    mem.reset()
    a, b = n // 3, n // 3 + 30 * 60 * d.rate
    clip, sec = timed(d.cut, a, b)
    rep.add("edit", "cut 30 minutes from the middle", f"{sec * 1000:.1f} ms", "cut_ms", round(sec * 1000, 2))
    _, sec = timed(d.paste, clip, 0)
    rep.add("edit", "paste them at the start", f"{sec * 1000:.1f} ms", "paste_ms", round(sec * 1000, 2))
    _, sec = timed(d.undo)
    _, sec2 = timed(d.undo)
    rep.add("edit", "undo × 2", f"{(sec + sec2) * 1000:.1f} ms", "undo_ms", round((sec + sec2) * 1000, 2))
    for _i in range(200):
        d.add_marker(_i * (n // 200), f"M{_i}")
    _, sec = timed(d.delete, n // 2, n // 2 + d.rate * 60)
    rep.add("edit", "delete 1 minute with 200 markers + regions", f"{sec * 1000:.1f} ms", "delete_markers_ms",
            round(sec * 1000, 2))
    d.undo()
    for _i in range(200):
        d.undo()
    assert d.frames == n and len(d.markers) == results["chapters"]
    rep.add("edit", "peak memory", mb(mem.peak()[0]))

    # analysis and processing
    mem.reset()
    st, sec = timed(analysis.analyze, d)
    rep.add("analysis", "LUFS + LRA + true peak of all of it", f"{sec:.1f} s ({d.duration() / sec:.0f}× real time), "
            f"{st.integrated:.1f} LUFS", "lufs_s", round(sec, 2))
    rep.add("analysis", "peak memory", mb(mem.peak()[0]), "lufs_rss", mem.peak()[0])
    mem.reset()
    (src, res), sec = timed(proc.normalize, d, 0, n, "loudness", -23.0, -1.0)
    d.replace(0, n, src, "normalize")
    rep.add("process", "normalize to −23 LUFS (two passes)", f"{sec:.1f} s ({d.duration() / sec:.0f}× real time), "
            f"{res.gain_db:+.2f} dB", "normalize_s", round(sec, 2))
    rep.add("process", "peak memory RSS / private", " / ".join(mb(x) for x in mem.peak()), "normalize_rss",
            mem.peak()[0])
    disk_after = folder_bytes(d.folder)
    d.undo()
    d.discard_redo()
    d.free_orphans()
    for key, values in (("limit", {"gain": 6.0, "ceiling": -1.0, "true_peak": True}),
                        (heavy, {"reduction": 12.0} if heavy == "nr" else {})):
        if key == "nr":
            fx.get("nr").profile = fx.noise_profile((x for _p, x in d.chunks(0, 20 * d.rate)), d.rate, d.channels)
        mem.reset()
        src, sec = timed(fx.render, d, 0, n, fx.get(key), values)
        d.replace(0, n, src, key)
        rep.add("effects", f"{fx.get(key).name.replace('fx_', '')} over all of it",
                f"{sec:.1f} s ({d.duration() / sec:.1f}× real time)", f"fx_{key}_s", round(sec, 2))
        rep.add("effects", f"{fx.get(key).name.replace('fx_', '')}: peak memory RSS / private",
                " / ".join(mb(x) for x in mem.peak()), f"fx_{key}_rss", mem.peak()[0])
        d.undo()
        d.discard_redo()
        d.free_orphans()
    gc_bytes = folder_bytes(d.folder)
    rep.add("disk", "working files after normalize", gb(disk_after), "disk_after_normalize", disk_after)
    rep.add("disk", "after the effects undone (their files freed)", gb(gc_bytes), "disk_after_gc", gc_bytes)

    # saving
    outdir = os.path.join(sess.path, "out")
    os.makedirs(outdir, exist_ok=True)
    for fmt, opt in (("flac", "24"), ("ac3", "640")):
        mem.reset()
        p = os.path.join(outdir, f"film.{fmt}")
        _, sec = timed(media.save, d, p, fmt, opt)
        size = os.path.getsize(p)
        back = ff.probe(p)
        rep.add("save", f"{media.FORMATS[fmt].name} {opt}", f"{sec:.1f} s ({d.duration() / sec:.0f}× real time), "
                f"{gb(size)}, {len(back.chapters)} chapters", f"save_{fmt}_s", round(sec, 2))
        rep.add("save", f"{media.FORMATS[fmt].name}: peak memory", mb(mem.peak()[0]), f"save_{fmt}_rss", mem.peak()[0])
        if abs(back.duration - d.duration()) > 1.0:
            raise RuntimeError(f"{fmt}: {back.duration} s, not {d.duration()} s")
        os.remove(p)

    # closing
    used = folder_bytes(sess.path)
    _, sec = timed(d.close)
    rep.add("close", "close the document (its working files)", f"{sec:.2f} s, {gb(used)} freed", "close_s", round(sec, 2))
    path = sess.path
    _, sec = timed(sess.close)
    left = os.path.isdir(path)
    rep.add("close", "the session folder removed", ("no!" if left else f"yes, {sec:.2f} s"), "session_removed", not left)
    free1 = shutil.disk_usage(os.path.dirname(path)).free
    rep.add("close", "disk space back", f"{gb(free1 - free0 + 0)} difference", "disk_diff", free1 - free0)
    rep.add("process", "peak memory of the whole run (RSS)", mb(max(r for r in [mem.peak()[0]] + [
        v for k, v in rep.data.items() if k.endswith("_rss") and isinstance(v, int)])), "rss_peak")
    mem.stop()
    md = rep.markdown(head)
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(rep.data, f, indent=2)
    if not keep:
        shutil.rmtree(os.path.join(out, "film"), ignore_errors=True)
    print("\n" + md)
    return 0 if not left else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--minutes", type=float, default=150.0)
    ap.add_argument("--dts", action="store_true")
    ap.add_argument("--heavy", default="nr", choices=("nr", "reverb", "declick", "eq", "comp"))
    ap.add_argument("--keep", action="store_true", help="keep the film for the next run")
    a = ap.parse_args()
    return run(a.out, a.minutes, a.dts, a.heavy, a.keep)


if __name__ == "__main__":
    sys.exit(main())
