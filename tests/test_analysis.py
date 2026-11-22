"""0.2: the analysis (peak, true peak, RMS, DC, clipping, EBU R128 loudness) checked against ffmpeg's ebur128
filter and against numpy, and the processing built on it: LUFS / peak normalization with a true-peak ceiling,
removing the DC offset, trimming the silence (pieces only), stereo ⇄ mono."""
import math
import os
import re
import subprocess
import time
import tracemalloc

import numpy as np
import pytest

from sound import analysis, media, proc, store
from sound import doc as D
from sound.analysis import to_db


def ffmpeg_r128(path: str) -> dict:
    """ffmpeg -af ebur128=peak=true: the summary (I, LRA, true peak) and the maximum M / S of its frame log."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-v", "verbose", "-i", path, "-af",
                        "ebur128=peak=true:framelog=verbose", "-f", "null", "-"], capture_output=True, text=True)
    e = r.stderr
    summ = e[e.rfind("Summary"):]
    return {"I": float(re.search(r"I:\s*(-?[\d.]+) LUFS", summ).group(1)),
            "LRA": float(re.search(r"LRA:\s*(-?[\d.]+) LU", summ).group(1)),
            "TP": float(re.search(r"Peak:\s*(-?[\d.]+) dBFS", summ).group(1)),
            "M": max(float(x) for x in re.findall(r"M:\s*(-?[\d.]+)", e)),
            "S": max(float(x) for x in re.findall(r"S:\s*(-?[\d.]+)", e))}


def make_doc(folder, x, rate=48000, labels=()):
    x = np.asarray(x, np.float32)
    d = D.Document(rate=rate, channels=x.shape[1], folder=folder, labels=tuple(labels))
    d.set_initial(store.from_array(folder, x))
    return d


def test_ebu_tech_3341_sine(loud_dir, session):
    """EBU Tech 3341 case 1: a 1 kHz stereo sine at −23 dBFS is −23.0 LUFS (M, S, I), ±0.1."""
    d = media.open_file(os.path.join(loud_dir, "ebu1k.wav"), session.folder())
    st = analysis.analyze(d)
    for v in (st.integrated, st.momentary_max, st.short_term_max):
        assert abs(v + 23.0) < 0.1, v
    assert st.lra < 0.1
    assert abs(to_db(st.peak) + 23.0) < 0.01 and abs(to_db(st.true_peak) + 23.0) < 0.02
    assert abs(st.threshold - (-33.0)) < 0.1                     # the relative gate 10 LU under


@pytest.mark.parametrize("name", ["ebu1k.wav", "pink.wav", "six.wav", "mono.wav", "peaky.wav", "dc_tone.wav"])
def test_loudness_matches_ffmpeg(loud_dir, session, name):
    """Integrated, LRA, max momentary and max short-term within 0.1 LU of ffmpeg ebur128 (it prints one decimal),
    the true peak within 0.2 dB."""
    path = os.path.join(loud_dir, name)
    d = media.open_file(path, session.folder())
    st = analysis.analyze(d)
    ref = ffmpeg_r128(path)
    got = {"I": st.integrated, "LRA": st.lra, "M": st.momentary_max, "S": st.short_term_max}
    for k, v in got.items():
        assert abs(v - ref[k]) <= 0.1, (k, v, ref[k])
    assert abs(to_db(st.true_peak) - ref["TP"]) <= 0.2, (to_db(st.true_peak), ref["TP"])


def test_channel_weights(tmp_path):
    """BS.1770: the LFE does not count, a surround channel counts 1.41 times (+1.49 dB) a front one."""
    n = 48000 * 3
    tone = (0.25 * np.sin(2 * np.pi * 1000 * np.arange(n) / 48000)).astype(np.float32)
    lab = ("L", "R", "C", "LFE", "Ls", "Rs")

    def only(ch):
        x = np.zeros((n, 6), np.float32)
        x[:, ch] = tone
        return analysis.analyze(make_doc(str(tmp_path), x, labels=lab)).integrated
    front, lfe, surround = only(0), only(3), only(4)
    assert lfe == float("-inf")
    assert abs(surround - front - 10 * math.log10(1.41)) < 0.01
    assert analysis.weights(lab).tolist() == [1, 1, 1, 0, 1.41, 1.41]


def test_true_peak_between_samples(tmp_path):
    """A sine at a quarter of the rate, 45° off the samples: every sample is at 0.707 of the peak, the true
    peak is 3 dB higher than the sample peak (measured inside the file: its abrupt start overshoots a little, as
    it would on a converter); a click's true peak is its sample."""
    n = np.arange(48000)
    x = (0.5 * np.sin(np.pi / 2 * n + np.pi / 4)).astype(np.float32)[:, None]
    d = make_doc(str(tmp_path), x)
    st = analysis.analyze(d, 1000, 40000)
    assert abs(st.peak - 0.5 * math.sqrt(0.5)) < 1e-6
    assert abs(to_db(st.true_peak) - to_db(0.5)) < 0.01
    assert analysis.analyze(d).true_peak > st.true_peak          # the start from silence
    y = np.zeros((48000, 1), np.float32)
    y[1000] = 0.8
    st = analysis.analyze(make_doc(str(tmp_path), y), loudness=False)
    assert abs(to_db(st.true_peak) - to_db(0.8)) < 0.001          # the filter keeps the samples


def test_true_peak_pruning_changes_nothing(loud_dir, session, monkeypatch):
    """Oversampling only the blocks within 6 dB of the peak so far gives the same as oversampling everything."""
    d = media.open_file(os.path.join(loud_dir, "mono.wav"), session.folder())
    fast = analysis.analyze(d, loudness=False).true_peak
    monkeypatch.setattr(analysis, "TP_MARGIN", 0.0)
    assert analysis.analyze(d, loudness=False).true_peak == fast


def test_basic_stats_against_numpy(loud_dir, session):
    for name in ("dc_tone.wav", "clipped.wav", "pink.wav"):
        d = media.open_file(os.path.join(loud_dir, name), session.folder())
        x = d.read(0, d.frames).astype(np.float64)
        st = analysis.analyze(d)
        for i, c in enumerate(st.channels):
            assert c.label == d.labels[i]
            assert c.peak == pytest.approx(np.abs(x[:, i]).max(), abs=1e-7)
            assert c.min == pytest.approx(x[:, i].min(), abs=1e-7) and c.max == pytest.approx(x[:, i].max(), abs=1e-7)
            assert c.rms == pytest.approx(np.sqrt((x[:, i] ** 2).mean()), rel=1e-9)
            assert c.dc == pytest.approx(x[:, i].mean(), abs=1e-9)
            assert c.clipped == int((np.abs(x[:, i]) >= analysis.CLIP).sum())
            assert c.true_peak >= c.peak
        assert st.frames == d.frames and st.rms == pytest.approx(np.sqrt((x ** 2).mean()), rel=1e-9)
    d = media.open_file(os.path.join(loud_dir, "dc_tone.wav"), session.folder())
    st = analysis.analyze(d)
    assert abs(st.channels[0].dc - 0.1) < 1e-4 and abs(st.channels[1].dc + 0.05) < 1e-4
    d = media.open_file(os.path.join(loud_dir, "clipped.wav"), session.folder())
    st = analysis.analyze(d)
    assert st.channels[0].clipped > 1000 and st.channels[1].clipped == 0     # 1.3 × sine clipped at 16 bit
    assert st.clipped == st.channels[0].clipped


def test_pieces_chunks_and_ranges_do_not_matter(loud_dir, session, tmp_path):
    """The same frames give the same figures however they lie: a document of many pieces against one source,
    random chunk sizes, a range of a document against that range on its own."""
    d = media.open_file(os.path.join(loud_dir, "pink.wav"), session.folder())
    ref = analysis.analyze(d)
    d.replace(100_000, 300_000, store.from_array(d.folder, d.read(100_000, 200_000)), "same frames")
    assert len(d.pieces) == 3
    st = analysis.analyze(d)
    assert st.integrated == pytest.approx(ref.integrated, abs=1e-9) and st.lra == pytest.approx(ref.lra, abs=1e-9)
    assert st.true_peak == pytest.approx(ref.true_peak, abs=1e-9)
    x = d.read(0, d.frames)
    an = analysis.Analyzer(d.rate, d.labels)
    rng = np.random.default_rng(3)
    i = 0
    while i < len(x):
        k = int(rng.integers(1, 7000))
        an.feed(x[i:i + k])
        i += k
    st = an.result()
    assert st.integrated == pytest.approx(ref.integrated, abs=1e-9)
    assert st.momentary_max == pytest.approx(ref.momentary_max, abs=1e-9)
    assert st.true_peak == pytest.approx(ref.true_peak, abs=1e-9)
    a, b = 123_456, 789_012
    part = analysis.analyze(d, a, b)
    alone = analysis.analyze(make_doc(str(tmp_path), d.read(a, b - a), d.rate, d.labels))
    assert part.integrated == pytest.approx(alone.integrated, abs=1e-9) and part.frames == b - a


def test_short_and_silent(tmp_path):
    d = make_doc(str(tmp_path), np.full((48000 // 4, 2), 0.1, np.float32))       # 250 ms: no 400 ms block
    st = analysis.analyze(d)
    assert st.integrated == float("-inf") and st.momentary_max == float("-inf") and st.lra == 0.0
    assert abs(st.peak - 0.1) < 1e-7
    st = analysis.analyze(make_doc(str(tmp_path), np.zeros((48000 * 5, 2), np.float32)))
    assert st.integrated == float("-inf") and st.peak == 0 and st.true_peak == 0 and st.clipped == 0


def test_cancel(loud_dir, session):
    d = media.open_file(os.path.join(loud_dir, "six.wav"), session.folder())
    calls = []

    def cb(f):
        calls.append(f)
        if len(calls) == 3:
            prog.cancel()
    prog = media.Progress(cb)
    with pytest.raises(media.Cancelled):
        analysis.analyze(d, progress=prog)


def test_long_51_analysis_in_little_memory(session):
    """Four minutes of 5.1 (280 MB as float) are measured in a few megabytes of memory."""
    w = store.SourceWriter(session.folder(), 6)
    t = np.arange(store.CHUNK) / 48000
    rng = np.random.default_rng(0)
    for i in range(48000 * 240 // store.CHUNK):
        x = np.empty((store.CHUNK, 6), np.float32)
        x[:] = (0.3 * np.sin(2 * np.pi * 220 * (t + i)))[:, None]
        x[:, 4:] += 0.05 * rng.standard_normal((store.CHUNK, 2)).astype(np.float32)
        w.append(x)
    src = w.finish()
    d = D.Document(rate=48000, channels=6, folder=os.path.dirname(src.path))
    d.set_initial(src)
    tracemalloc.start()
    t0 = time.monotonic()
    st = analysis.analyze(d)
    dt = time.monotonic() - t0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 40 * 1024 * 1024, f"peak {peak / 1e6:.1f} MB"
    assert math.isfinite(st.integrated) and st.lra < 1.0
    print(f"4 min 5.1: {dt:.1f} s, peak memory {peak / 1e6:.1f} MB")


# ── processing ───────────────────────────────────────────────
def _saved_r128(d, folder, name="out.wav"):
    path = os.path.join(folder, name)
    media.save(d, path, "wav", "32f")
    return ffmpeg_r128(path)


def test_normalize_to_lufs(loud_dir, session, tmp_path):
    d = media.open_file(os.path.join(loud_dir, "pink.wav"), session.folder())
    src, res = proc.normalize(d, 0, d.frames, "loudness", -16.0, -1.0)
    d.replace(0, d.frames, src, "normalize")
    assert not res.limited and abs(res.after + 16) < 1e-9
    assert abs(analysis.analyze(d).integrated + 16.0) < 0.01
    ref = _saved_r128(d, str(tmp_path))
    assert abs(ref["I"] + 16.0) <= 0.1 and ref["TP"] <= -1.0


def test_normalize_to_lufs_hits_the_ceiling(loud_dir, session):
    """Loud clicks over a quiet tone: −14 LUFS would put the true peak far over −1 dBTP, so the gain stops at the
    ceiling and the result says so."""
    d = media.open_file(os.path.join(loud_dir, "peaky.wav"), session.folder())
    before = analysis.analyze(d)
    src, res = proc.normalize(d, 0, d.frames, "loudness", -14.0, -1.0)
    d.replace(0, d.frames, src, "normalize")
    assert res.limited and res.after < -14.0
    after = analysis.analyze(d)
    assert abs(to_db(after.true_peak) + 1.0) < 0.01
    assert abs(after.integrated - res.after) < 0.01
    assert abs(res.gain_db - (-1.0 - to_db(before.true_peak))) < 1e-9


def test_normalize_peak_and_true_peak(loud_dir, session):
    d = media.open_file(os.path.join(loud_dir, "mono.wav"), session.folder())
    src, res = proc.normalize(d, 0, d.frames, "peak", -1.0, true_peak=True)
    d.replace(0, d.frames, src, "normalize")
    st = analysis.analyze(d)
    assert abs(to_db(st.true_peak) + 1.0) < 0.01 and to_db(st.peak) < -1.0
    src, res = proc.normalize(d, 0, d.frames, "peak", -3.0)
    d.replace(0, d.frames, src, "normalize")
    assert abs(to_db(float(d.peak().max())) + 3.0) < 1e-3
    silent = make_doc(session.folder(), np.zeros((48000, 2), np.float32))
    for mode in ("peak", "loudness"):
        with pytest.raises(ValueError):
            proc.normalize(silent, 0, silent.frames, mode, -16.0)


def test_normalize_a_selection_only(loud_dir, session):
    d = media.open_file(os.path.join(loud_dir, "pink.wav"), session.folder())
    a, b = 44100 * 10, 44100 * 20
    head = d.read(0, a).copy()
    src, _res = proc.normalize(d, a, b, "loudness", -20.0)
    d.replace(a, b, src, "normalize")
    np.testing.assert_array_equal(d.read(0, a), head)
    assert abs(analysis.analyze(d, a, b).integrated + 20.0) < 0.01


def test_remove_dc(loud_dir, session):
    d = media.open_file(os.path.join(loud_dir, "dc_tone.wav"), session.folder())
    src, dc = proc.remove_dc(d, 0, d.frames)
    assert abs(dc[0] - 0.1) < 1e-4 and abs(dc[1] + 0.05) < 1e-4
    d.replace(0, d.frames, src, "dc")
    st = analysis.analyze(d, loudness=False, true_peak=False)
    assert all(abs(c.dc) < 1e-6 for c in st.channels)
    assert abs(st.channels[0].peak - 0.3) < 1e-3
    d.undo()
    assert abs(analysis.analyze(d, loudness=False, true_peak=False).channels[0].dc - 0.1) < 1e-4


def test_trim_silence_edits_only_the_pieces(loud_dir, session):
    """1 s of silence, 2 s of tone, 0.5 s of silence: the edges go with a 10 ms margin; no sound is written (the
    document uses the same one source), undo gives it all back."""
    d = media.open_file(os.path.join(loud_dir, "gap.wav"), session.folder())
    n0 = d.frames
    srcs = d.sources()
    m = 441
    a, b = proc.sound_edges(d, 0, d.frames, -60.0, m)
    assert abs(a - (44100 - m)) <= 2 and abs(b - (3 * 44100 + m)) <= 2
    d.keep(0, d.frames, a, b, "trim silence")
    assert d.frames == b - a and d.sources() == srcs and d.sel is None and d.cursor == 0
    assert np.abs(d.read(0, m - 2)).max() < 1e-3 and np.abs(d.read(m + 10, 100)).max() > 0.01
    d.undo()
    assert d.frames == n0
    # within a selection only its edges go; the selection becomes what is kept
    d.sel = (22050, 3 * 44100 + 11025)
    a2, b2 = proc.sound_edges(d, *d.sel, -60.0, 0)
    d.keep(*d.sel, a2, b2)
    assert d.frames == n0 - (a2 - 22050) - (3 * 44100 + 11025 - b2) and d.sel == (22050, 22050 + b2 - a2)
    # all quiet, or nothing to cut
    assert proc.sound_edges(d, 0, 20000, -60.0) is None
    assert proc.sound_edges(d, 0, d.frames, 0.0) is None
    e = make_doc(session.folder(), np.full((1000, 1), 0.5, np.float32))
    assert proc.sound_edges(e, 0, e.frames, -60.0) == (0, 1000)


def test_trim_silence_of_a_long_silence_is_quick(session):
    """Ten minutes of silence before a beep: found from the peak levels without reading the silence."""
    w = store.SourceWriter(session.folder(), 2)
    z = np.zeros((store.CHUNK, 2), np.float32)
    for _ in range(48000 * 600 // store.CHUNK):
        w.append(z)
    w.append(np.full((100, 2), 0.5, np.float32))
    w.append(z)
    src = w.finish()
    d = D.Document(rate=48000, channels=2, folder=os.path.dirname(src.path))
    d.set_initial(src)
    t0 = time.monotonic()
    a, b = proc.sound_edges(d, 0, d.frames, -60.0)
    assert time.monotonic() - t0 < 2.0
    assert (a, b) == (48000 * 600 // store.CHUNK * store.CHUNK, 48000 * 600 // store.CHUNK * store.CHUNK + 100)


def test_stereo_mono(media_dir, session):
    d = media.open_file(os.path.join(media_dir, "stereo16.wav"), session.folder())
    x = d.read(0, d.frames)
    for mode, want in (("mix", (x[:, 0] + x[:, 1]) / 2), ("left", x[:, 0]), ("right", x[:, 1])):
        src = proc.stereo_to_mono(d, mode)
        assert src.channels == 1
        np.testing.assert_allclose(np.asarray(src.data)[:, 0], want, atol=1e-7)
    src = proc.stereo_to_mono(d, "mix")
    d.replace(0, d.frames, src, "mono", sel=None, channels=1, layout="mono", labels=("M",))
    assert d.channels == 1 and d.labels == ("M",)
    src = proc.mono_to_stereo(d)
    d.replace(0, d.frames, src, "stereo", sel=None, channels=2, layout="stereo", labels=("L", "R"))
    y = d.read(0, d.frames)
    np.testing.assert_array_equal(y[:, 0], y[:, 1])
    d.undo()
    d.undo()
    assert d.channels == 2
    np.testing.assert_array_equal(d.read(0, d.frames), x)
    with pytest.raises(ValueError):
        proc.mono_to_stereo(d)


def test_loudness_gain():
    assert proc.loudness_gain(-20.0, -8.0, -14.0, -1.0) == (6.0, False)
    assert proc.loudness_gain(-20.0, -3.0, -14.0, -1.0) == (2.0, True)
    assert proc.loudness_gain(-10.0, -0.5, -23.0, -1.0) == (-13.0, False)
