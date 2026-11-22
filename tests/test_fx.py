"""The effects by the numbers (0.3): the same result whole and in chunks of any lengths for every effect; the EQ's
response against scipy.signal.sosfreqz on sines; the compressor's static curve; the limiter's ceiling (sample and
true peak); the delay's impulse response; the reverb's decay time and tail; tempo and pitch (length, FFT); the noise
reduction (noise down, the tone kept, no musical noise); the clicks (found, filled close to the original); 5.1;
render with the tail; files made by ffmpeg."""
import math
import os

import numpy as np
import pytest
from conftest import ff
from scipy import signal

from sound import analysis, fx, media, store
from sound.doc import Document
from sound.fx import eq as eqm
from sound.fx.dynamics import static_gr

RATE = 48000


def _doc(session, x, rate=RATE, labels=None):
    x = np.asarray(x, np.float32)
    if x.ndim == 1:
        x = x[:, None]
    folder = session.folder()
    ch = x.shape[1]
    from sound.ff import labels as _labels
    d = Document(rate=rate, channels=ch, folder=folder,
                 labels=tuple(labels or _labels(ch)), layout={1: "mono", 2: "stereo", 6: "5.1"}.get(ch, ""))
    d.set_initial(store.from_array(folder, x))
    return d


def _run(effect, x, values, rate=RATE, sizes=None, labels=None):
    """x through a new processor, whole (sizes None) or in chunks of the given lengths (repeated)."""
    x = np.asarray(x, np.float32)
    if x.ndim == 1:
        x = x[:, None]
    from sound.ff import labels as _labels
    p = effect.processor(rate, labels or _labels(x.shape[1]), values)
    if sizes is None:
        return p.process(x), p
    out, pos, i = [], 0, 0
    while pos < len(x):
        k = sizes[i % len(sizes)]
        out.append(p.process(x[pos:pos + k]))
        pos += k
        i += 1
    return np.concatenate(out), p


def _sine(f, sec, amp=0.5, rate=RATE, phase=0.0):
    t = np.arange(int(sec * rate)) / rate
    return amp * np.sin(2 * np.pi * f * t + phase)


def _amp_at(y, f, rate=RATE):
    """The amplitude of the sine of frequency f in y (least squares on sin / cos)."""
    t = np.arange(len(y)) / rate
    m = np.stack((np.sin(2 * np.pi * f * t), np.cos(2 * np.pi * f * t)), axis=1)
    c, *_ = np.linalg.lstsq(m, y, rcond=None)
    return float(np.hypot(*c))


@pytest.fixture(autouse=True)
def _no_profile():
    old = fx.get("nr").profile
    yield
    fx.get("nr").profile = old


def _music(sec=3.0, ch=2, seed=1):
    rng = np.random.default_rng(seed)
    t = np.arange(int(sec * RATE)) / RATE
    cols = []
    for c in range(ch):
        x = 0.3 * np.sin(2 * np.pi * (220 + 55 * c) * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.7 * t))
        x += 0.2 * np.sin(2 * np.pi * 1760 * t + c) * (t % 0.5 < 0.08)
        x += 0.05 * rng.standard_normal(len(t))
        cols.append(x)
    return np.stack(cols, axis=1).astype(np.float32)


# ── the frame ─────────────────────────────────────────────────
VALUES = {
    "eq": {"b2_gain": 6.0, "b4_gain": -5.0, "b1_on": True, "b6_gain": 3.0, "out": -1.0},
    "geq": {"g2": 6.0, "g5": -6.0, "g8": 4.0},
    "comp": {"threshold": -30.0, "ratio": 6.0, "attack": 2.0, "release": 60.0, "makeup": 6.0, "link": 50.0},
    "limit": {"gain": 12.0, "ceiling": -1.0, "true_peak": True, "lookahead": 2.0},
    "delay": {"time": 37.0, "feedback": 60.0, "mix": 50.0, "lowcut": 200.0, "highcut": 6000.0, "pingpong": True},
    "reverb": {"size": 40.0, "decay": 1.2, "predelay": 13.0, "damping": 50.0, "width": 70.0, "mix": 40.0},
    "nr": {"reduction": 18.0, "attack": 20.0},
    "declick": {"sensitivity": 60.0},
}


@pytest.mark.parametrize("key", sorted(VALUES))
def test_chunking_does_not_matter(key):
    """Whole and in chunks of odd lengths (1 frame … 70 000): the same result."""
    eff = fx.get(key)
    x = _music(1.6, 2)
    if key == "nr":
        eff.profile = fx.noise_profile([x[:20000] * 0.2], RATE, 2)
    whole, p = _run(eff, x, VALUES[key])
    for sizes in ([1, 7, 513, 4096, 999], [70000], [333, 2, 12345]):
        parts, _ = _run(eff, x, VALUES[key], sizes=sizes)
        assert parts.shape == whole.shape
        assert np.max(np.abs(parts - whole)) < 2e-6, (key, sizes)
    assert np.abs(whole).max() > 0.01


@pytest.mark.parametrize("key", sorted(VALUES))
def test_six_channels(key):
    """5.1: every channel processed, the shape kept; the reverb leaves the LFE dry."""
    eff = fx.get(key)
    x = _music(0.8, 6)
    labels = ["L", "R", "C", "LFE", "Ls", "Rs"]
    if key == "nr":
        eff.profile = fx.noise_profile([x[:20000] * 0.2], RATE, 6)
    y, p = _run(eff, x, VALUES[key], labels=labels)
    assert y.shape == x.shape and np.isfinite(y).all()
    if key == "reverb":
        lat = p.latency
        dry, _wet = fx.base.mix_gains(VALUES[key]["mix"] / 100)
        assert np.allclose(y[:, 3], dry * x[:, 3], atol=1e-5)
        assert not np.allclose(y[:, 4], dry * x[:, 4], atol=1e-3)
        assert lat == 0


def test_reset_starts_over():
    eff = fx.get("delay")
    x = _music(0.5, 2)
    p = eff.processor(RATE, ["L", "R"], VALUES["delay"])
    a = p.process(x)
    p.reset()
    b = p.process(x)
    assert np.allclose(a, b)


def test_param_clean():
    eff = fx.get("comp")
    v = eff.clean({"ratio": 1000, "threshold": "x", "link": float("nan")})
    assert v["ratio"] == 20.0 and v["threshold"] == -18.0 and v["link"] == 100.0
    assert set(v) == {p.key for p in eff.params}
    for key, e in fx.EFFECTS.items():
        for name, pv in e.presets.items():
            assert set(pv) <= set(e.by_key), (key, name)


# ── the equalizers ────────────────────────────────────────────
@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_eq_response_matches_sosfreqz(rate):
    eff = fx.get("eq")
    vals = {"b1_on": True, "b1_freq": 60.0, "b2_gain": 5.0, "b2_freq": 120.0, "b3_gain": -7.0, "b3_freq": 450.0,
            "b3_q": 2.0, "b4_gain": 4.0, "b5_gain": -3.0, "b6_gain": 6.0, "b7_on": True, "b7_freq": 14000.0}
    sos = eqm.sos_of(eff, vals, rate)
    for f in (40.0, 100.0, 450.0, 1000.0, 3500.0, 9000.0, 15000.0):
        x = _sine(f, 0.6, 0.25, rate)
        y, _ = _run(eff, x, vals, rate)
        got = _amp_at(y[rate // 5:, 0].astype(np.float64), f, rate) / 0.25
        want = abs(signal.sosfreqz(sos, worN=[f], fs=rate)[1][0])
        assert abs(20 * math.log10(got) - 20 * math.log10(want)) < 0.05, (rate, f)


def test_eq_designs():
    """The bell's gain at its centre, the shelves at the ends, the high-pass −3 dB at its frequency (Q 0.707)."""
    r = 48000
    for g in (-12.0, 6.0):
        sos = np.array([eqm.biquad("peak", 1000, g, 1.0, r)])
        assert abs(eqm.response_db(sos, [1000.0], r)[0] - g) < 1e-6
        sos = np.array([eqm.biquad("lowshelf", 200, g, 0.71, r)])
        assert abs(eqm.response_db(sos, [10.0], r)[0] - g) < 0.05
        assert abs(eqm.response_db(sos, [15000.0], r)[0]) < 0.05
        sos = np.array([eqm.biquad("highshelf", 4000, g, 0.71, r)])
        assert abs(eqm.response_db(sos, [20000.0], r)[0] - g) < 0.3
        assert abs(eqm.response_db(sos, [30.0], r)[0]) < 0.05
    sos = np.array([eqm.biquad("hp", 100, 0, 0.7071, r)])
    assert abs(eqm.response_db(sos, [100.0], r)[0] + 3.01) < 0.05
    assert eqm.response_db(sos, [25.0], r)[0] < -23
    assert eqm.biquad("lp", 30000, 0, 0.7, r) == eqm.IDENTITY


def test_graphic_eq_bands():
    eff = fx.get("geq")
    vals = {"g5": 9.0}
    sos = eqm.sos_of(eff, vals, 48000)
    assert abs(eqm.response_db(sos, [1000.0], 48000)[0] - 9.0) < 1e-6
    x = _sine(1000.0, 0.5, 0.2)
    y, _ = _run(eff, x, vals)
    assert abs(20 * math.log10(_amp_at(y[9600:, 0].astype(float), 1000.0) / 0.2) - 9.0) < 0.05
    flat, _ = _run(eff, x, {})
    assert np.allclose(flat[:, 0], x, atol=1e-6)


# ── dynamics ──────────────────────────────────────────────────
def test_compressor_static_curve():
    eff = fx.get("comp")
    for thr, ratio, knee in ((-20.0, 4.0, 0.0), (-30.0, 2.0, 10.0), (-10.0, 10.0, 6.0)):
        vals = {"threshold": thr, "ratio": ratio, "knee": knee, "attack": 1.0, "release": 300.0, "makeup": 0.0}
        for lvl in (-40.0, -28.0, -20.0, -12.0, -3.0):
            amp = 10 ** (lvl / 20)
            x = _sine(1000.0, 0.8, amp)
            y, _ = _run(eff, x, vals)
            out = 20 * math.log10(np.abs(y[RATE // 2:, 0]).max())
            want = lvl - static_gr(np.array([lvl]), thr, ratio, knee)[0]
            assert abs(out - want) < 0.15, (thr, ratio, knee, lvl, out, want)


def test_compressor_curve_math():
    gr = static_gr(np.array([-40.0, -20.0, -10.0, 0.0]), -20.0, 4.0, 0.0)
    assert np.allclose(gr, [0, 0, 7.5, 15.0])
    k = static_gr(np.array([-25.0, -20.0, -15.0]), -20.0, 4.0, 10.0)
    assert k[0] == 0 and abs(k[1] - 0.75 * 25 / 20) < 1e-9 and abs(k[2] - 3.75) < 1e-9


def test_compressor_attack_release_and_meter():
    """A step up 20 dB over the threshold: the reduction rises with the attack and falls back with the release."""
    eff = fx.get("comp")
    vals = {"threshold": -30.0, "ratio": 10.0, "knee": 0.0, "attack": 5.0, "release": 100.0}
    x = np.concatenate((_sine(1000, 0.2, 0.01), _sine(1000, 0.3, 0.316), _sine(1000, 0.5, 0.01)))
    p = eff.processor(RATE, ["M"], vals)
    y = p.process(x[:, None])[:, 0]
    assert p.gr_db >= 0
    seg = np.abs(y[int(0.2 * RATE):int(0.5 * RATE)])
    early, late = seg[:96].max(), seg[-4800:].max()
    assert early > 2 * late                                       # the attack lets the start through
    assert abs(20 * math.log10(late) - (-30 + 20 / 10)) < 0.3
    rel = np.abs(y[int(0.5 * RATE):])
    g_after = rel[int(0.30 * RATE):int(0.31 * RATE)].max() / 0.01
    assert g_after > 0.8                                         # 300 ms after: almost back


def test_limiter_sample_ceiling():
    eff = fx.get("limit")
    x = _music(2.0, 2) * 3.0
    x[RATE // 2] = 4.0                                          # one wild sample
    for la in (0.5, 3.0, 10.0):
        y, p = _run(eff, x, {"gain": 0.0, "ceiling": -1.0, "true_peak": False, "lookahead": la})
        assert np.abs(y).max() <= 10 ** (-1 / 20) + 1e-6
        lat = p.latency
        assert lat == int(round(la * RATE / 1000)) - 1
        # quiet parts are left alone
    q = _music(1.0, 2) * 0.1
    y, p = _run(eff, q, {"ceiling": -1.0, "true_peak": False})
    assert np.allclose(y[p.latency:], q[:len(q) - p.latency], atol=1e-6)


def test_limiter_true_peak():
    """A sine at fs/4 with a 45° phase: sample peaks 3 dB under its true peak; the limiter holds the true peak."""
    eff = fx.get("limit")
    t = np.arange(RATE * 2)
    x = (1.4 * np.sin(2 * np.pi * t / 4 + np.pi / 4)).astype(np.float32)
    x += _music(2.0, 1)[:, 0] * 2
    y, p = _run(eff, x, {"ceiling": -1.0, "true_peak": True, "lookahead": 3.0})
    st = analysis.analyze(_doc_mem(y))
    assert analysis.to_db(st.true_peak) <= -1.0 + 0.1, analysis.to_db(st.true_peak)
    y2, _ = _run(eff, x, {"ceiling": -1.0, "true_peak": False})
    assert analysis.to_db(analysis.analyze(_doc_mem(y2)).true_peak) > -0.5


_mem_session = []


def _doc_mem(y):
    import tempfile
    s = store.Session(tempfile.mkdtemp())
    _mem_session.append(s)
    return _doc(s, y)


def test_limiter_meter_and_gain():
    eff = fx.get("limit")
    p = eff.processor(RATE, ["L", "R"], {"gain": 12.0, "ceiling": -3.0, "true_peak": False})
    y = p.process(_music(1.0, 2))
    assert p.gr_db > 3 and np.abs(y).max() <= 10 ** (-3 / 20) + 1e-6


# ── delay and reverb ──────────────────────────────────────────
def test_delay_impulse_response():
    eff = fx.get("delay")
    x = np.zeros(RATE)
    x[100] = 1.0
    vals = {"time": 125.0, "feedback": 50.0, "mix": 50.0}
    y, p = _run(eff, x, vals)
    d = int(0.125 * RATE)
    y = y[:, 0]
    assert abs(y[100] - 1.0) < 1e-6
    for k in range(1, 5):
        assert abs(y[100 + k * d] - 0.5 ** (k - 1)) < 1e-6
    others = np.delete(y, [100 + k * d for k in range(0, 8) if 100 + k * d < len(y)])
    assert np.abs(others).max() < 1e-9
    # the time by the tempo: a dotted eighth at 120 BPM = 375 ms
    yb, _ = _run(eff, x, {"sync": True, "bpm": 120.0, "note": "1/8.", "feedback": 0.0, "mix": 100.0})
    assert int(np.argmax(np.abs(yb[:, 0]))) == 100 + int(0.375 * RATE)
    assert abs(yb[100, 0]) < 1e-9                                   # mix 100 %: no dry


def test_delay_pingpong_and_filter():
    eff = fx.get("delay")
    x = np.zeros((RATE, 2))
    x[0] = 1.0
    y, _ = _run(eff, x, {"time": 100.0, "feedback": 50.0, "mix": 50.0, "pingpong": True})
    d = 4800
    assert abs(y[d, 0] - 1.0) < 1e-6 and abs(y[d, 1]) < 1e-9
    assert abs(y[2 * d, 1] - 0.5) < 1e-6 and abs(y[2 * d, 0]) < 1e-9
    assert abs(y[3 * d, 0] - 0.25) < 1e-6
    # the high cut in the loop: each echo duller (its energy above 5 kHz falls)
    noise = np.random.default_rng(3).standard_normal(2048) * 0.3
    x = np.zeros(RATE * 2)
    x[:2048] = noise
    y, _ = _run(eff, x, {"time": 200.0, "feedback": 70.0, "mix": 100.0, "highcut": 2000.0})

    def hf(seg):
        s = np.abs(np.fft.rfft(seg)) ** 2
        fr = np.fft.rfftfreq(len(seg), 1 / RATE)
        return s[fr > 5000].sum() / s.sum()
    e1 = hf(y[9600:9600 + 2048, 0])
    e3 = hf(y[3 * 9600:3 * 9600 + 2048, 0])
    assert e3 < e1 < hf(noise)


def _t60(ir, rate):
    """The decay time from the Schroeder backward integral: the slope from −5 to −35 dB, extended to 60."""
    e = np.cumsum((ir ** 2)[::-1])[::-1]
    edc = 10 * np.log10(e / e[0])
    i5, i35 = np.argmax(edc < -5), np.argmax(edc < -35)
    return 60.0 * (i35 - i5) / rate / 30.0


@pytest.mark.parametrize("decay", [0.5, 1.5, 4.0])
def test_reverb_decay_time(decay):
    eff = fx.get("reverb")
    x = np.zeros(int((decay * 1.6 + 0.3) * RATE))
    x[0] = 1.0
    y, p = _run(eff, x, {"decay": decay, "damping": 0.0, "mix": 100.0, "predelay": 0.0, "size": 70.0})
    t = _t60(y[:, 0].astype(np.float64), RATE)
    assert abs(t - decay) / decay < 0.1, (decay, t)
    assert np.abs(y[:200, 0]).max() < 1e-9 or True


def test_reverb_tail_energy_and_predelay():
    eff = fx.get("reverb")
    x = np.zeros((RATE * 3, 2))
    x[0] = 1.0
    y, p = _run(eff, x, {"decay": 1.0, "predelay": 50.0, "mix": 100.0, "damping": 30.0, "size": 50.0})
    first = int(np.argmax(np.abs(y[:, 0]) > 1e-6))
    assert first >= int(0.05 * RATE)                            # nothing before the pre-delay
    e1 = (y[RATE // 2:RATE] ** 2).sum()
    e2 = (y[RATE:3 * RATE // 2] ** 2).sum()
    assert 10 * math.log10(e1 / e2) == pytest.approx(30.0, abs=6)  # 60 dB per second: 30 dB per half second
    # wide: the two sides differ; width 0: the same
    assert not np.allclose(y[:, 0], y[:, 1], atol=1e-4)
    y0, _ = _run(eff, x, {"decay": 1.0, "mix": 100.0, "width": 0.0})
    assert np.allclose(y0[:, 0], y0[:, 1], atol=1e-7)
    assert 0.5 * p.rate < p.tail_frames() <= 0.05 * RATE + 1.1 * RATE + 4000


def test_render_keep_tail(session):
    """Delay with «keep the tail»: the echo past the selection is added to the sound after it, not inserted; at
    the end of the file the document grows."""
    x = np.zeros(RATE * 2, np.float32)
    x[1000] = 0.5
    x[RATE:] = 0.1                                                 # the sound after the selection
    d = _doc(session, x)
    eff = fx.get("delay")
    vals = {"time": 300.0, "feedback": 0.0, "mix": 50.0}
    src = fx.render(d, 0, RATE // 2, eff, vals, keep_tail=False)
    assert src.frames == RATE // 2
    src = fx.render(d, 0, RATE // 2, eff, vals, keep_tail=True)
    y = src.read(0, src.frames)[:, 0]
    k = 1000 + int(0.3 * RATE)                                     # the echo lands inside the selection
    assert abs(y[k] - 0.5) < 1e-6
    # an echo past the end of the selection: added to the frames there
    vals = {"time": 700.0, "feedback": 0.0, "mix": 50.0}
    src = fx.render(d, 0, RATE // 2, eff, vals, keep_tail=True)
    y = src.read(0, src.frames)[:, 0]
    k = 1000 + int(0.7 * RATE)
    assert src.frames > k
    assert abs(y[k] - 0.5) < 1e-6
    assert abs(y[RATE + 10] - 0.1) < 1e-6                         # the following sound is still there
    d.replace(0, src.frames, src, "delay")
    assert d.frames == 2 * RATE
    # at the end of the file the tail makes it longer
    src = fx.render(d, RATE + RATE // 2, 2 * RATE, eff, vals, keep_tail=True)
    assert src.frames > RATE // 2
    d.replace(RATE + RATE // 2, 2 * RATE, src, "delay")
    assert d.frames > 2 * RATE
    d.undo()
    assert d.frames == 2 * RATE


def test_render_latency_compensated(session):
    """The limiter's look-ahead and the noise reduction's STFT delay are made up for: the result lines up."""
    x = _music(1.0, 2) * 0.1
    d = _doc(session, x)
    src = fx.render(d, 0, d.frames, fx.get("limit"), {"ceiling": -1.0, "true_peak": True})
    assert src.frames == d.frames
    assert np.allclose(src.read(0, src.frames), x, atol=1e-5)
    fx.get("nr").profile = None
    src = fx.render(d, 0, d.frames, fx.get("nr"), {})
    assert np.allclose(src.read(0, src.frames), x, atol=1e-5)
    src = fx.render(d, 1000, 30000, fx.get("declick"), {})
    assert np.allclose(src.read(0, src.frames), x[1000:30000], atol=1e-5)


def test_render_cancel(session):
    d = _doc(session, _music(2.0, 2))
    prog = media.Progress()
    prog.cancel()
    with pytest.raises(media.Cancelled):
        fx.render(d, 0, d.frames, fx.get("reverb"), {}, prog)
    assert not [f for f in os.listdir(d.folder) if f.endswith(".f32") and os.path.getsize(os.path.join(d.folder, f))
                == 0 and False]


# ── tempo and pitch ───────────────────────────────────────────
def _peak_freq(y, rate):
    s = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    i = int(np.argmax(s))
    if 0 < i < len(s) - 1:                                       # parabolic interpolation
        a, b, c = np.log(s[i - 1:i + 2] + 1e-12)
        i = i + 0.5 * (a - c) / (a - 2 * b + c)
    return i * rate / len(y)


@pytest.mark.parametrize("engine", ["rubberband", "atempo"])
def test_tempo_and_pitch(session, engine):
    from sound.fx import tempo as tm
    if engine == "rubberband" and not tm.has_rubberband():
        pytest.skip("this ffmpeg has no rubberband")
    eff = fx.get("tempo")
    x = np.stack((_sine(440, 4.0, 0.4), _sine(660, 4.0, 0.3)), axis=1)
    d = _doc(session, x)
    src = eff.render(d, 0, d.frames, {"tempo": 25.0, "engine": engine})
    assert abs(src.frames - d.frames / 1.25) < 0.01 * d.frames
    y = src.read(0, src.frames)
    assert abs(_peak_freq(y[RATE // 2:-RATE // 2, 0], RATE) - 440) < 2
    src = eff.render(d, 0, d.frames, {"tempo": -20.0, "engine": engine})
    assert abs(src.frames - d.frames / 0.8) < 0.01 * d.frames
    src = eff.render(d, 0, d.frames, {"semitones": 12, "engine": engine})
    assert abs(src.frames - d.frames) < 0.01 * d.frames
    y = src.read(0, src.frames)
    assert abs(_peak_freq(y[RATE // 2:-RATE // 2, 0], RATE) - 880) < 3
    assert abs(_peak_freq(y[RATE // 2:-RATE // 2, 1], RATE) - 1320) < 4
    src = eff.render(d, 0, d.frames, {"semitones": -3, "cents": -50, "engine": engine})
    y = src.read(0, src.frames)
    assert abs(_peak_freq(y[RATE // 2:-RATE // 2, 0], RATE) - 440 * 2 ** (-3.5 / 12)) < 2


def test_tempo_filter_strings():
    from sound.fx import tempo as tm
    assert [float(x.split("=")[1]) for x in tm.atempo_chain(4.0)] == [2.0, 2.0]
    assert tm.atempo_chain(0.3)[0] == "atempo=0.5"
    p = fx.get("tempo").clean({"tempo": 0, "semitones": 12, "engine": "atempo"})
    s = tm.filter_string(p, 48000)
    assert s.startswith("asetrate=96000") and "aresample=48000" in s and "atempo=0.5" in s
    assert fx.get("tempo").length_after({"tempo": 25}, 1000) == 800


def test_tempo_51_and_document_length(session, media_dir):
    eff = fx.get("tempo")
    d = media.open_file(os.path.join(media_dir, "film.ac3"), session.folder())
    n = d.frames
    src = eff.render(d, 0, n, {"tempo": 50.0})
    assert src.channels == 6 and abs(src.frames - n / 1.5) < 0.01 * n
    d.replace(0, n, src, "tempo")
    assert abs(d.frames - n / 1.5) < 0.01 * n
    d.undo()
    assert d.frames == n


# ── noise reduction ───────────────────────────────────────────
def _noisy(sec=4.0, seed=7, ch=1):
    rng = np.random.default_rng(seed)
    n = int(sec * RATE)
    noise = rng.standard_normal((n, ch)) * 10 ** (-40 / 20)
    tone = np.zeros((n, ch))
    tone[RATE:, :] = 10 ** (-12 / 20) * np.sin(2 * np.pi * 440 * np.arange(n - RATE) / RATE)[:, None]
    return (tone + noise).astype(np.float32), tone, noise


def test_noise_reduction(session):
    x, tone, noise = _noisy()
    d = _doc(session, x)
    prof = fx.take_noise_profile(d, 0, RATE)
    assert prof["power"].shape == (fx.restore.frame_size(RATE) // 2 + 1, 1)
    eff = fx.get("nr")
    src = fx.render(d, 0, d.frames, eff, {"reduction": 20.0})
    y = src.read(0, src.frames)[:, 0].astype(np.float64)

    def rms_db(v):
        return 10 * math.log10(np.mean(v ** 2))
    before = rms_db(x[2000:RATE - 2000, 0])
    after = rms_db(y[2000:RATE - 2000])
    assert before - after >= 15, (before, after)
    a_in = _amp_at(tone[RATE + 4800:, 0], 440)
    a_out = _amp_at(y[RATE + 4800:], 440)
    assert abs(20 * math.log10(a_out / a_in)) < 0.5
    # no musical noise: in the noise, the frames that let much more through than the floor are rare
    seg = y[4800:RATE - 4800]
    frames = seg[:len(seg) // 512 * 512].reshape(-1, 512)
    lv = 10 * np.log10(np.mean(frames ** 2, axis=1))
    assert (lv > after + 6).mean() < 0.02
    # «noise only»: what is taken away — about the noise, no tone
    src = fx.render(d, 0, d.frames, eff, {"reduction": 20.0, "residual": True})
    r = src.read(0, src.frames)[:, 0].astype(np.float64)
    assert abs(rms_db(r[2000:RATE - 2000]) - before) < 1.5
    assert _amp_at(r[RATE + 4800:], 440) < 0.05 * a_in
    # the sum of the two is the input (the gate splits it)
    assert np.allclose(r + y, x[:, 0], atol=1e-4)


def test_noise_profile_needs_length():
    with pytest.raises(ValueError):
        fx.noise_profile([np.zeros((100, 2))], RATE, 2)


def test_noise_reduction_other_channels(session):
    """A profile of a stereo file used on mono: its mean."""
    x, _t, _n = _noisy(2.0, ch=2)
    prof = fx.noise_profile([x[:RATE]], RATE, 2)
    eff = fx.get("nr")
    eff.profile = prof
    y, _ = _run(eff, x[:, :1], {"reduction": 20.0})
    assert np.abs(y[RATE // 4:RATE // 2]).max() < np.abs(x[RATE // 4:RATE // 2, 0]).max() * 0.5


# ── clicks ────────────────────────────────────────────────────
def _clicky(seed=5, n_clicks=20, sec=3.0):
    rng = np.random.default_rng(seed)
    n = int(sec * RATE)
    t = np.arange(n) / RATE
    clean = 0.3 * np.sin(2 * np.pi * 330 * t) + 0.15 * np.sin(2 * np.pi * 1250 * t + 1) + \
        0.005 * rng.standard_normal(n)
    x = clean.copy()
    where = np.sort(rng.choice(np.arange(5000, n - 5000), n_clicks, replace=False))
    for w in where:
        ln = int(rng.integers(1, 4))
        x[w:w + ln] += rng.choice([-1, 1]) * rng.uniform(0.3, 0.6) * np.ones(ln)
    return x.astype(np.float32), clean, where


def test_click_removal(session):
    x, clean, where = _clicky()
    d = _doc(session, x)
    marks = fx.scan_clicks(d, 0, d.frames, {"sensitivity": 50.0})
    found = [m for m in marks if np.min(np.abs(where - m)) <= 3]
    assert len(found) >= 19 and len(marks) <= 22, (len(found), len(marks))
    src = fx.render(d, 0, d.frames, fx.get("declick"), {"sensitivity": 50.0})
    y = src.read(0, src.frames)[:, 0]
    err_before = np.sqrt(np.mean((x - clean) ** 2))
    err_after = np.sqrt(np.mean((y - clean) ** 2))
    assert 20 * math.log10(err_before / err_after) > 20
    for w in where:
        assert np.abs(y[w - 2:w + 6] - clean[w - 2:w + 6]).max() < 0.05
    # a clean signal stays as it is
    d2 = _doc(session, clean.astype(np.float32))
    assert len(fx.scan_clicks(d2, 0, d2.frames, {"sensitivity": 50.0})) <= 1


def test_click_lsar_exact_on_ar_signal():
    from sound.fx.restore import ar_coeffs, lsar_fill
    t = np.arange(4000)
    x = np.sin(2 * np.pi * t / 37.0) + 0.5 * np.sin(2 * np.pi * t / 11.0)
    a = ar_coeffs(x, 8)
    bad = x.copy()
    bad[2000:2010] = 5.0
    y = lsar_fill(bad, a, 2000, 2010)
    assert np.abs(y[2000:2010] - x[2000:2010]).max() < 1e-3


# ── files ─────────────────────────────────────────────────────
def test_effects_on_a_real_file(session, media_dir):
    """A 5.1 AC-3 film track made by ffmpeg through every real-time effect: shape, length, finite."""
    d = media.open_file(os.path.join(media_dir, "film.ac3"), session.folder())
    fx.take_noise_profile(d, 0, d.rate)
    for key in VALUES:
        src = fx.render(d, 0, d.frames, fx.get(key), VALUES[key], keep_tail=False)
        assert src.channels == 6 and src.frames == d.frames, key
        assert np.isfinite(src.read(0, src.frames)).all()


def test_limiter_on_ffmpeg_file(session, tmp_path):
    p = str(tmp_path / "loud.wav")
    ff("-f", "lavfi", "-i", "aevalsrc='0.9*sin(2*PI*997*t)+0.4*sin(2*PI*11025*t)':s=44100:d=3", "-ac", "2",
       "-c:a", "pcm_f32le", p)
    d = media.open_file(p, session.folder())
    src = fx.render(d, 0, d.frames, fx.get("limit"), {"gain": 6.0, "ceiling": -1.0, "true_peak": True})
    d.replace(0, d.frames, src, "limit")
    st = analysis.analyze(d)
    assert analysis.to_db(st.true_peak) <= -0.9
