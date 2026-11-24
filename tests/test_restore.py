"""Restoration (2.0, block 2) by the numbers: hum (found, followed as it drifts, cut by 20 dB and more, the music
around kept), clipping (the clipped peaks rebuilt close to the original, past the clip level; found as Apply finds
them), the de-esser (sibilant bursts down, the voice kept), crackle (dense small impulses taken away, clean music
untouched), adaptive noise reduction (no sample: the noise between the notes down, the notes kept; «music» keeps a
held note). Each through fx.render on a document — the latency made up for."""
import math

import numpy as np
import pytest
from scipy import signal

from sound import fx, store
from sound.doc import Document
from sound.fx import repair

RATE = 48000


def _doc(session, x, rate=RATE):
    x = np.asarray(x, np.float32)
    if x.ndim == 1:
        x = x[:, None]
    from sound.ff import labels
    d = Document(rate=rate, channels=x.shape[1], folder=session.folder(), labels=tuple(labels(x.shape[1])),
                 layout={1: "mono", 2: "stereo", 6: "5.1"}.get(x.shape[1], ""))
    d.set_initial(store.from_array(d.folder, x))
    return d


def _render(session, x, key, values):
    d = _doc(session, x)
    src = fx.render(d, 0, d.frames, fx.get(key), values)
    return src.read(0, src.frames).astype(np.float64)


def _amp(y, f, rate=RATE):
    t = np.arange(len(y)) / rate
    m = np.stack((np.sin(2 * np.pi * f * t), np.cos(2 * np.pi * f * t)), axis=1)
    c, *_ = np.linalg.lstsq(m, y, rcond=None)
    return float(np.hypot(*c))


def _db(a, b):
    return 20 * math.log10(max(a, 1e-12) / max(b, 1e-12))


def _hum(sec, f_start, f_end, harmonics=(1, 2, 3, 5, 7), amp=0.03):
    t = np.arange(int(sec * RATE)) / RATE
    f = f_start + (f_end - f_start) * t / sec                           # a slow drift
    ph = 2 * np.pi * np.cumsum(f) / RATE
    return sum(amp / k ** 0.5 * np.sin(k * ph + k) for k in harmonics), f


def _music(sec, seed=3):
    rng = np.random.default_rng(seed)
    t = np.arange(int(sec * RATE)) / RATE
    return 0.25 * np.sin(2 * np.pi * 220 * t) + 0.12 * np.sin(2 * np.pi * 1330 * t) + 0.005 * rng.standard_normal(len(t))


# ── hum ───────────────────────────────────────────────────────
@pytest.mark.parametrize("base,f0,f1", [("auto", 50.3, 49.7), ("50", 49.8, 50.2), ("auto", 60.2, 59.9)])
def test_hum_followed_and_cut(session, base, f0, f1):
    sec = 8.0
    hum, f = _hum(sec, f0, f1)
    mus = _music(sec)
    y = _render(session, mus + hum, "dehum", {"base": base, "harmonics": 12, "width": 2.0, "depth": 40.0})[:, 0]
    # every harmonic, a second at a time (the frequency moves), after the first second
    t = np.arange(len(y)) / RATE
    for s in range(2, int(sec) - 1):
        sl = slice(s * RATE, (s + 1) * RATE)
        fm = float(f[sl].mean())
        ph = 2 * np.pi * np.cumsum(f) / RATE
        for k in (1, 2, 3, 5, 7):
            m = np.stack((np.sin(k * ph[sl]), np.cos(k * ph[sl])), axis=1)
            before = np.hypot(*np.linalg.lstsq(m, hum[sl], rcond=None)[0])
            after = np.hypot(*np.linalg.lstsq(m, y[sl] - mus[sl], rcond=None)[0])
            assert _db(before, after) > 20, (s, k, fm, _db(before, after))
    for ft in (220, 1330):                                              # the music: within 0.3 dB
        assert abs(_db(_amp(y[RATE:], ft), _amp(mus[RATE:], ft))) < 0.3
    del t


def test_hum_detect_and_residual(session):
    hum, _f = _hum(6.0, 60.15, 60.15)
    d = _doc(session, _music(6.0) + hum)
    got = fx.detect_hum(d, 0, d.frames)
    assert got["base"] == 60.0 and abs(got["f0"] - 60.15) < 0.03
    d2 = _doc(session, _music(6.0))
    assert fx.detect_hum(d2, 0, d2.frames)["f0"] == 0.0                # no hum: nothing found
    y = _render(session, _music(6.0) + hum, "dehum", {"residual": True})[:, 0]
    sl = slice(2 * RATE, 5 * RATE)
    assert _amp(y[sl], 60.15) > 0.8 * _amp(hum[sl], 60.15) and _amp(y[sl], 220) < 0.01


def test_hum_no_hum_is_left_alone(session):
    mus = _music(4.0)
    y = _render(session, mus, "dehum", {})[:, 0]
    assert np.max(np.abs(y - mus)) < 1e-6


def test_notch_shape():
    sos = repair.notch_sos(50.0, 3, 2.0, 40.0, RATE)
    w, h = signal.sosfreqz(sos, worN=[25.0, 50.0, 100.0, 150.0, 75.0, 1000.0], fs=RATE)
    g = 20 * np.log10(np.abs(h))
    assert all(abs(g[i] + 40) < 0.5 for i in (1, 2, 3)) and abs(g[0]) < 0.2 and abs(g[4]) < 0.2 and abs(g[5]) < 0.01


# ── clipping ──────────────────────────────────────────────────
def _clipped(sec=2.0, level=0.55):
    t = np.arange(int(sec * RATE)) / RATE
    x = 0.5 * np.sin(2 * np.pi * 330 * t) + 0.35 * np.sin(2 * np.pi * 550 * t + 1) + 0.2 * np.sin(2 * np.pi * 97 * t)
    return x, np.clip(x, -level, level)


def test_declip_rebuilds_peaks(session):
    x, c = _clipped()
    lvl = 0.55
    y = _render(session, c, "declip", {"headroom": 0.0})[:, 0]
    hot = np.abs(c) >= lvl
    err_clip = np.sqrt(np.mean((c[hot] - x[hot]) ** 2))
    err_fix = np.sqrt(np.mean((y[hot] - x[hot]) ** 2))
    assert err_fix < 0.25 * err_clip, (err_fix, err_clip)
    assert np.all(np.abs(y[hot]) >= lvl * 0.999)                          # past the clip level
    assert np.max(np.abs(y[~hot] - c[~hot])) < 1e-6                     # the rest untouched
    z = _render(session, c, "declip", {"headroom": 6.0})[:, 0]
    assert np.allclose(z, y * 10 ** (-6 / 20), atol=1e-6)


def test_declip_scan_and_set_level(session):
    x, c = _clipped(1.0, 0.4)
    d = _doc(session, np.stack((c, x * 0.3), axis=1))                     # the right channel is not clipped
    res = fx.scan_clips(d, 0, d.frames, {})
    runs = repair.find_runs(c, 0.4 * 10 ** (-0.05 / 20), 3, 480)
    assert res["runs"] == len(runs) and res["marks"] == [r[0] for r in runs]
    assert res["peak_db"] > 20 * math.log10(0.4) + 1 and res["need_db"] == 0.0
    # a set level under the clipping finds the same runs; one over it finds none
    at = {"auto": False, "threshold": 20 * math.log10(0.4)}
    assert fx.scan_clips(d, 0, d.frames, at)["runs"] == len(runs)
    assert fx.scan_clips(d, 0, d.frames, {"auto": False, "threshold": -6.0})["runs"] == 0


def test_declip_loud_clipping_needs_headroom(session):
    x, c = _clipped(1.0, 0.95)
    d = _doc(session, c * 1.0)
    res = fx.scan_clips(d, 0, d.frames, {})
    assert res["runs"] > 50 and res["need_db"] > 0.3


def test_declip_spline_long_runs(session):
    x, c = _clipped(1.0, 0.3)
    y = _render(session, c, "declip", {"method": "spline", "headroom": 0.0, "max_len": 20.0})[:, 0]
    hot = np.abs(c) >= 0.3
    assert np.sqrt(np.mean((y[hot] - x[hot]) ** 2)) < 0.6 * np.sqrt(np.mean((c[hot] - x[hot]) ** 2))


# ── de-esser ──────────────────────────────────────────────────
def _voice(sec=3.0):
    rng = np.random.default_rng(7)
    t = np.arange(int(sec * RATE)) / RATE
    v = 0.3 * np.sin(2 * np.pi * 180 * t) + 0.1 * np.sin(2 * np.pi * 540 * t)
    sos = signal.butter(4, [5000, 9000], btype="bandpass", fs=RATE, output="sos")
    s = signal.sosfilt(sos, rng.standard_normal(len(t)))
    gate = ((t % 1.0) > 0.6) & ((t % 1.0) < 0.75)                       # an «s» every second
    s = 0.6 * s / np.abs(s).max() * gate
    return v, s


@pytest.mark.parametrize("mode", ["split", "wide"])
def test_deesser(session, mode):
    v, s = _voice()
    vals = {"freq": 6500.0, "threshold": -30.0, "reduction": 12.0, "mode": mode}
    y = _render(session, v + s, "deess", vals)[:, 0]
    sos = signal.butter(4, [5000, 9000], btype="bandpass", fs=RATE, output="sos")
    hi_in = np.sqrt(np.mean(signal.sosfilt(sos, v + s) ** 2))
    hi_out = np.sqrt(np.mean(signal.sosfilt(sos, y) ** 2))
    assert _db(hi_in, hi_out) > 6
    quiet = slice(int(0.1 * RATE), int(0.5 * RATE))                     # between the «s»: the voice as it was
    assert np.max(np.abs(y[quiet] - v[quiet])) < 1e-3
    if mode == "split":                                                   # the low voice kept under the «s» too
        burst = slice(int(1.62 * RATE), int(1.73 * RATE))
        assert abs(_db(_amp(y[burst], 180), _amp(v[burst], 180))) < 0.5
    lis = _render(session, v + s, "deess", {**vals, "listen": True})[:, 0]
    assert np.max(np.abs(lis[quiet])) < 1e-3 and np.max(np.abs(lis)) > 0.05


def test_deesser_meter(session):
    v, s = _voice(1.0)
    p = fx.get("deess").processor(RATE, ["M"], {"threshold": -30.0, "reduction": 9.0})
    p.process((v + s)[:, None])
    assert p.gr_db == pytest.approx(9.0, abs=0.01)


# ── crackle ───────────────────────────────────────────────────
def _crackle(n, rate_per_s=600, seed=5):
    rng = np.random.default_rng(seed)
    c = np.zeros(n)
    k = int(n / RATE * rate_per_s)
    pos = rng.integers(10, n - 10, k)
    for p in pos:
        w = rng.integers(1, 4)
        c[p:p + w] += rng.uniform(0.03, 0.15) * rng.choice((-1, 1))
    return c


def test_decrackle(session):
    mus = _music(3.0)
    cr = _crackle(len(mus))
    y = _render(session, mus + cr, "decrackle", {"sensitivity": 60.0})[:, 0]
    before = np.sqrt(np.mean(cr ** 2))
    after = np.sqrt(np.mean((y - mus) ** 2))
    assert _db(before, after) > 8, _db(before, after)
    clean = _render(session, mus, "decrackle", {"sensitivity": 60.0})[:, 0]
    assert np.sqrt(np.mean((clean - mus) ** 2)) < 0.01 * np.sqrt(np.mean(mus ** 2))
    only = _render(session, mus + cr, "decrackle", {"sensitivity": 60.0, "residual": True})[:, 0]
    assert np.allclose(only, (mus + cr) - y, atol=1e-5)


def test_decrackle_six_channels_and_amount(session):
    mus = _music(1.0)
    x = np.stack([mus + _crackle(len(mus), seed=s) for s in range(6)], axis=1)
    y = _render(session, x, "decrackle", {"amount": 0.0})
    assert np.allclose(y, x, atol=1e-6)


# ── adaptive noise reduction ──────────────────────────────────
def test_adaptive_noise_reduction_voice(session):
    rng = np.random.default_rng(11)
    sec = 6.0
    t = np.arange(int(sec * RATE)) / RATE
    on = (t % 1.0) < 0.5                                                 # «words» and pauses
    tone = 0.3 * np.sin(2 * np.pi * 700 * t) * on
    noise = 0.02 * rng.standard_normal(len(t))
    y = _render(session, tone + noise, "nr_auto", {"mode": "voice", "reduction": 18.0})[:, 0]
    gaps = (~on) & (t > 2.0) & ((t % 1.0) > 0.65) & ((t % 1.0) < 0.95)
    assert _db(np.sqrt(np.mean(noise[gaps] ** 2)), np.sqrt(np.mean(y[gaps] ** 2))) > 10
    words = on & (t > 2.0) & ((t % 1.0) > 0.1) & ((t % 1.0) < 0.4)
    assert abs(_db(_amp(y[words], 700), _amp(tone[words], 700))) < 1.0


def test_adaptive_noise_reduction_music_keeps_a_held_note(session):
    rng = np.random.default_rng(12)
    sec = 8.0
    t = np.arange(int(sec * RATE)) / RATE
    held = (t % 2.8) < 2.5                                               # quiet notes of 2.5 s, short breaths
    note = 0.05 * np.sin(2 * np.pi * 440 * t) * held
    noise = 0.01 * rng.standard_normal(len(t))
    y = _render(session, note + noise, "nr_auto", {"mode": "music", "reduction": 12.0})[:, 0]
    for k in (1, 2):                                                     # the end of a note, where the voice mode
        sl = slice(int((k * 2.8 + 1.5) * RATE), int((k * 2.8 + 2.4) * RATE))   # would have taken it for noise
        assert abs(_db(_amp(y[sl], 440), _amp(note[sl], 440))) < 1.0
    v = _render(session, note + noise, "nr_auto", {"mode": "voice", "reduction": 12.0})[:, 0]
    sl = slice(int(4.3 * RATE), int(5.2 * RATE))
    assert _db(_amp(note[sl], 440), _amp(v[sl], 440)) > 3


@pytest.mark.parametrize("mode", ["voice", "music"])
def test_adaptive_estimate_is_the_noise_level(mode):
    """On noise alone the estimate is the noise's mean power per bin, as a noise sample gives it (within 1.5 dB)."""
    from sound.fx import restore
    rng = np.random.default_rng(1)
    x = (0.01 * rng.standard_normal(8 * RATE))[:, None]
    p = fx.get("nr_auto").processor(RATE, ["M"], {"mode": mode})
    p.process(x)
    prof = restore.noise_profile([x], RATE, 1)["power"][10:-10, 0]
    r = np.median(p.noise_now()[0, 10:-10] / prof)
    assert abs(10 * math.log10(r)) < 1.5


# ── batch steps and the Restoration set ───────────────────────
def test_restoration_keys():
    from sound import batch
    for k in ("nr_auto", "decrackle", "declip", "dehum", "deess"):
        assert k in fx.ORDER and k in fx.RESTORE and k in batch.FX_STEPS
        assert batch.step_defaults(k) == fx.get(k).defaults()
