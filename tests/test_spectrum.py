"""The spectrum (0.4): sines read at their frequency and level with every window (the window's gain taken out), the
result does not depend on how the frames are cut into chunks, a short range, the mix of the channels, the octave
smoothing, notes, the main peak; and the spectrogram's tiles against a direct computation, its cache bounded, a
2.5-hour 5.1 document scrolled through without the memory growing, tiles kept after an edit elsewhere."""
import os
import tracemalloc

import numpy as np
import pytest

from sound import doc as D
from sound import spectrogram as sg
from sound import spectrum as spm
from sound import store

RATE = 48000


def _tones(secs=4, rate=RATE):
    t = np.arange(int(secs * rate)) / rate
    f0 = rate / 8192 * 300                       # the middle of a bin of an 8192 FFT (1757.8 Hz)
    return np.stack([0.5 * np.sin(2 * np.pi * f0 * t), 0.1 * np.sin(2 * np.pi * 1000.3 * t)], 1), f0


@pytest.mark.parametrize("win", spm.WINDOWS)
def test_sine_levels(win):
    x, f0 = _tones()
    w = spm.Welch(RATE, 8192, win, 2, ["L", "R"])
    w.feed(x)
    res = w.result()
    lv = res.levels()
    f, v = spm.peak(res.freqs, lv[0], win=win)
    assert abs(f - f0) < 0.5 and abs(v - 20 * np.log10(0.5)) < 0.01            # bin-centred: exact
    f, v = spm.peak(res.freqs, lv[1], win=win)
    tol = {"hann": 0.5, "blackmanharris": 0.3, "flattop": 0.05}[win]           # between bins: the scalloping
    assert abs(f - 1000.3) < 3.0 and abs(v - 20 * np.log10(0.1)) < tol
    assert res.segments == (len(x) - 8192) // 4096 + 1


def test_chunks_do_not_matter():
    x, _f0 = _tones(3)
    ref = spm.Welch(RATE, 4096, "hann", 2)
    ref.feed(x)
    for size in (1, 777, 4096, 65536):
        if size == 1:
            parts = [x[:5], x[5:9000], x[9000:9001], x[9001:]]
        else:
            parts = [x[i:i + size] for i in range(0, len(x), size)]
        w = spm.Welch(RATE, 4096, "hann", 2)
        for p in parts:
            w.feed(p)
        assert np.allclose(w.result().power, ref.result().power, rtol=1e-9, atol=1e-18)


def test_document_spectrum(session):
    x, f0 = _tones(5)
    d = D.Document(rate=RATE, channels=2, folder=session.folder())
    src = store.from_array(d.folder, x.astype(np.float32))
    d.set_initial(src)
    d.cut(RATE, 2 * RATE)
    clip = d.copy(0, RATE)
    d.paste(clip, 3 * RATE)                       # several pieces
    st = spm.Settings(fft=8192)
    seen = []
    res = spm.spectrum(d, 0, d.frames, st, lambda f: seen.append(f))
    w = spm.Welch(RATE, 8192, "hann", 2)
    w.feed(d.read(0, d.frames))
    assert np.allclose(res.power, w.result().power, rtol=1e-6, atol=1e-15)
    assert seen and seen[-1] == 1.0
    st.mode = "sum"
    res = spm.spectrum(d, RATE, 2 * RATE, st)
    assert res.labels == ["Σ"] and res.power.shape[0] == 1
    f, v = spm.peak(res.freqs, res.levels()[0])
    assert abs(f - f0) < 1 and abs(v - 20 * np.log10(0.25)) < 0.01            # the mix: (0.5 + 0) / 2


def test_short_range_and_smoothing():
    t = np.arange(1000) / RATE
    x = (0.5 * np.sin(2 * np.pi * 3000 * t))[:, None]
    w = spm.Welch(RATE, 8192, "hann", 1)
    w.feed(x)
    res = w.result()
    assert res.segments == 1
    f, v = spm.peak(res.freqs, res.levels()[0])
    assert abs(f - 3000) < 30 and abs(v - 20 * np.log10(0.5)) < 1.5
    noise = np.random.default_rng(0).standard_normal((RATE * 2, 1)) * 0.1
    w = spm.Welch(RATE, 4096, "hann", 1)
    w.feed(noise)
    r = w.result()
    raw = r.levels()[0][100:1500]
    sm = r.levels(3)[0][100:1500]
    assert np.std(np.diff(sm)) < 0.5 * np.std(np.diff(raw))                    # smoother
    assert abs(np.mean(sm) - np.mean(10 * np.log10(np.mean(10 ** (raw / 10))))) < 1.5


def test_notes_and_csv():
    assert spm.note(440.0) == "A4 +0 ct"
    assert spm.note(261.6256) == "C4 +0 ct"
    assert spm.note(27.5) == "A0 +0 ct"
    assert spm.note(446.4) == "A4 +25 ct" and spm.note(432.0) == "A4 −32 ct"
    assert spm.note(5.0) == ""
    x, _f0 = _tones(1)
    w = spm.Welch(RATE, 1024, "hann", 2, ["L", "R"])
    w.feed(x)
    txt = spm.to_csv(w.result())
    lines = txt.strip().split("\r\n")
    assert lines[0] == "Frequency (Hz),L (dB),R (dB)" and len(lines) == 1 + 513
    st = spm.Settings.from_dict({"fft": 999, "window": "nope", "db_lo": -500, "mode": "sum", "smooth": 6})
    assert (st.fft, st.window, st.db_lo, st.mode, st.smooth) == (8192, "hann", -200.0, "sum", 6)


# ── the spectrogram ───────────────────────────────────────────
def _direct_column(d, col, spp, fft, rows, scale, ch):
    """What one column of a tile must hold, worked out directly."""
    m = max(1, min(sg.MAX_SEG, int(np.ceil(spp / fft))))
    w = spm.window("hann", fft)
    acc = 0
    for j in range(m):
        c = col * spp + (j + 0.5) * spp / m
        s0 = int(np.floor(c - fft / 2))
        x = np.zeros(fft)
        lo, hi = max(0, s0), min(d.frames, s0 + fft)
        if hi > lo:
            x[lo - s0:hi - s0] = d.read(lo, hi - lo)[:, ch]
        acc = acc + np.abs(np.fft.rfft(x * w)) ** 2 * (2 / w.sum()) ** 2
    pw = (acc / m)[None, :]
    r = sg.to_rows(pw, sg.row_map(rows, d.rate, fft, scale))
    return sg.quantize(10 * np.log10(np.maximum(r, 1e-30)))[0, ::-1]


@pytest.mark.parametrize("spp,scale", [(37.5, "log"), (900.0, "lin"), (20000.0, "mel")])
def test_tiles_match_direct(session, spp, scale):
    rng = np.random.default_rng(1)
    n = RATE * 12
    t = np.arange(n) / RATE
    x = np.stack([0.4 * np.sin(2 * np.pi * (200 + 300 * t) * t), 0.05 * rng.standard_normal(n)], 1)
    d = D.Document(rate=RATE, channels=2, folder=session.folder())
    d.set_initial(store.from_array(d.folder, x.astype(np.float32)))
    st = sg.Settings(fft=1024, scale=scale)
    order, jobs = sg.jobs_for_view(d, 3.3 * RATE, spp, 700, 90, st)
    j = jobs[order[0]]
    arr = sg.compute_tile(j.reader, j.frames, j.rate, j.spp, j.tile, j.rows, j.fft, j.scale, j.chs)
    assert arr.shape == (2, 90, sg.TILE) and arr.dtype == np.uint8
    for k in (0, 17, 128, 255):
        col = j.tile * sg.TILE + k
        if col * spp >= d.frames:
            assert not arr[:, :, k].any()                         # past the end: nothing
            continue
        for c in (0, 1):
            ref = _direct_column(d, col, spp, 1024, 90, scale, c)
            assert np.abs(arr[c, :, k].astype(int) - ref.astype(int)).max() <= 1


def test_cache_is_bounded():
    cache = sg.TileCache(limit_bytes=1_000_000)
    for i in range(200):
        cache.put(("k", i), np.zeros((2, 100, sg.TILE), np.uint8))
    assert cache.bytes <= 1_000_000 and len(cache) == 1_000_000 // (2 * 100 * sg.TILE)
    assert cache.get(("k", 0)) is None and cache.get(("k", 199)) is not None


def test_long_51_memory(session):
    """A 2.5-hour 5.1 document (one minute of sound repeated) scrolled and zoomed through: the cache stays under its
    limit and the memory does not grow with the length."""
    x = (0.3 * np.sin(2 * np.pi * np.arange(RATE * 60)[:, None] * np.array([100, 200, 400, 800, 1600, 3200]) / RATE))
    src = store.from_array(session.folder(), x.astype(np.float32))
    d = D.Document(rate=RATE, channels=6, folder=session.folder())
    d.set_initial(src)
    d._set([(src, 0, src.frames)] * 150)
    assert d.frames == RATE * 9000
    st = sg.Settings(fft=2048)
    cache = sg.TileCache(limit_bytes=8 << 20)
    eng = sg.Engine(cache, workers=2)
    tracemalloc.start()
    try:
        import time
        peak_first = None
        for step, (start, spp) in enumerate([(0, d.frames / 1400)] +
                                            [(p * d.frames / 40, sp) for p in range(40) for sp in (4000.0, 60.0)]):
            order, jobs = sg.jobs_for_view(d, start, spp, 1400, 80, st)
            eng.want([jobs[i] for i in order])
            t0 = time.monotonic()
            while not eng.idle() and time.monotonic() - t0 < 60:
                time.sleep(0.01)
            assert eng.idle()
            cur, peak = tracemalloc.get_traced_memory()
            if step == 4:
                peak_first = cur
            assert cache.bytes <= cache.limit
        cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        eng.stop()
    assert eng.errors == 0 and eng.computed > 100
    assert cur - peak_first < 12 << 20                       # the cache's 8 MB at most, and no more
    assert peak < 200 << 20


def test_edit_keeps_unchanged_tiles(session):
    x, _f0 = _tones(30)
    d = D.Document(rate=RATE, channels=2, folder=session.folder())
    d.set_initial(store.from_array(d.folder, x.astype(np.float32)))
    st = sg.Settings(fft=1024)
    spp = 200.0
    _o, before = sg.jobs_for_view(d, 0, spp, 1400, 60, st)
    d.delete(25 * RATE, 26 * RATE)                       # after the visible part
    _o, after = sg.jobs_for_view(d, 0, spp, 1400, 60, st)
    assert all(before[i].key == after[i].key for i in before)
    d.silence(RATE // 2, RATE)                            # inside the first tile
    _o, after2 = sg.jobs_for_view(d, 0, spp, 1400, 60, st)
    assert after2[0].key != before[0].key and after2[max(after2)].key == before[max(before)].key
    d.undo()
    _o, back = sg.jobs_for_view(d, 0, spp, 1400, 60, st)
    assert all(back[i].key == after[i].key for i in back)


def test_color_table_and_ticks():
    tab = sg.color_table("magma", -100, 0)
    assert len(tab) == 256 and tab[0] == 0 and all(c >> 24 == 0xFF for c in tab[1:])
    q_lo = int(sg.quantize(np.array([-150.0]))[0])
    q_hi = int(sg.quantize(np.array([10.0]))[0])
    assert tab[q_lo] == tab[1] and tab[q_hi] == tab[255]             # clipped to the range's ends
    assert sg.freq_ticks(48000, "log", 300)[:3] == [50.0, 100.0, 200.0]
    for scale in sg.SCALES:
        f = sg.f_of_frac(0.37, 44100, scale)
        assert abs(float(sg.y_frac(f, 44100, scale)) - 0.37) < 1e-9
    st = sg.Settings.from_dict({"fft": 3, "scale": "x", "palette_dark": "nope", "db_lo": -10})
    assert (st.fft, st.scale, st.palette_dark, st.db_lo) == (2048, "log", "magma", -20.0)
    assert os.path.basename(__file__)
