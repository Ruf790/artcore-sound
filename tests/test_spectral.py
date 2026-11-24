"""Spectral editing (2.0, block 2): a tone inside the selection removed, the sound outside it untouched (bit for bit
outside the region, −90 dB and better inside it away from the selection), no click at the region's ends; rectangle,
lasso and brush rasterized where they were drawn; add / take away; channels; attenuate / boost by N dB; fill from the
neighbours; the preview of only what is selected; a long document processed a block at a time; one undo step."""
import math

import numpy as np
import pytest

from sound import spectral, store
from sound.doc import Document
from sound.spectral import Params, Shape, SpecSel

RATE = 48000


def _doc(session, x):
    x = np.asarray(x, np.float32)
    if x.ndim == 1:
        x = x[:, None]
    from sound.ff import labels
    d = Document(rate=RATE, channels=x.shape[1], folder=session.folder(), labels=tuple(labels(x.shape[1])),
                 layout={1: "mono", 2: "stereo"}.get(x.shape[1], ""))
    d.set_initial(store.from_array(d.folder, x))
    return d


def _amp(y, f):
    t = np.arange(len(y)) / RATE
    m = np.stack((np.sin(2 * np.pi * f * t), np.cos(2 * np.pi * f * t)), axis=1)
    c, *_ = np.linalg.lstsq(m, y, rcond=None)
    return float(np.hypot(*c))


def _sig(sec=4.0, seed=2):
    rng = np.random.default_rng(seed)
    t = np.arange(int(sec * RATE)) / RATE
    bed = 0.2 * np.sin(2 * np.pi * 300 * t) + 0.01 * rng.standard_normal(len(t))
    tone = 0.2 * np.sin(2 * np.pi * 3000 * t) * ((t >= 1.0) & (t < 2.0))        # 3 kHz for one second
    return bed, tone


def _apply(d, sel, **kw):
    a, b, src = spectral.apply(d, sel, Params(**kw))
    y = d.read(0, d.frames).astype(np.float64).copy()
    y[a:b] = src.read(0, src.frames)
    return a, b, y[:, 0]


def test_tone_erased_inside_untouched_outside(session):
    bed, tone = _sig()
    x = bed + tone
    d = _doc(session, x)
    sel = SpecSel(RATE, "log", (0,), [Shape("rect", [(int(0.95 * RATE), 2500.0), (int(2.05 * RATE), 3600.0)])])
    a, b, y = _apply(d, sel, action="erase", feather_ms=5.0, feather_hz=20.0)
    assert a < int(0.95 * RATE) and b > int(2.05 * RATE) and b - a < 1.4 * RATE       # only the region around it
    mid = slice(int(1.1 * RATE), int(1.9 * RATE))
    assert _amp(y[mid], 3000) < 1e-4 * 0.2                                         # the tone: −80 dB and more
    assert abs(_amp(y[mid], 300) / _amp(bed[mid], 300) - 1) < 1e-4                 # the bed under it kept
    x32 = x.astype(np.float32).astype(np.float64)
    assert np.array_equal(y[:a], x32[:a]) and np.array_equal(y[b:], x32[b:])     # outside the region: as it was
    # inside the region but away from the selection (before the tone): −90 dB and better
    near = slice(a, int(0.9 * RATE))
    assert np.max(np.abs(y[near] - x32[near])) < 10 ** (-90 / 20)
    # no click at the ends of the region: the first difference there is no bigger than the sound's own
    dy = np.abs(np.diff(y))
    own = np.abs(np.diff(x32)).max()
    for e in (a, b):
        assert dy[e - 4:e + 4].max() <= own * 1.001


def test_attenuate_and_boost_by_db(session):
    bed, tone = _sig()
    d = _doc(session, bed + tone)
    sel = SpecSel(RATE, "log", (0,), [Shape("rect", [(int(0.9 * RATE), 2000.0), (int(2.1 * RATE), 4000.0)])])
    mid = slice(int(1.2 * RATE), int(1.8 * RATE))
    for action, db in (("attenuate", 12.0), ("boost", 6.0)):
        _a, _b, y = _apply(d, sel, action=action, db=db, feather_ms=5.0, feather_hz=20.0)
        want = -12.0 if action == "attenuate" else 6.0
        got = 20 * math.log10(_amp(y[mid], 3000) / 0.2)
        assert abs(got - want) < 0.1, (action, got)
        assert abs(_amp(y[mid], 300) / 0.2 - 1) < 1e-3


def test_lasso_and_brush_where_drawn(session):
    """A lasso and a brush over the tone take it away; a lasso drawn beside it changes nothing there."""
    bed, tone = _sig()
    d = _doc(session, bed + tone)
    mid = slice(int(1.2 * RATE), int(1.8 * RATE))
    poly = [(int(0.95 * RATE), 2600.0), (int(1.5 * RATE), 2300.0), (int(2.05 * RATE), 2600.0),
            (int(2.05 * RATE), 3500.0), (int(1.5 * RATE), 3900.0), (int(0.95 * RATE), 3500.0)]
    _a, _b, y = _apply(d, SpecSel(RATE, "log", (0,), [Shape("lasso", poly)]), action="erase")
    assert _amp(y[mid], 3000) < 1e-3 * 0.2
    sel = SpecSel(RATE, "log", (0,))
    fr = float(sel.frac(3000.0))
    sel.add(Shape("brush", [(int(0.95 * RATE), 3000.0), (int(2.05 * RATE), 3000.0)], rx=0.03 * RATE, ry=0.04))
    assert len(sel.shapes[0].pts) > 10                                  # the stroke filled in
    _a, _b, y = _apply(d, sel, action="erase")
    assert _amp(y[mid], 3000) < 1e-3 * 0.2 and abs(_amp(y[mid], 300) / 0.2 - 1) < 1e-3
    beside = [(int(0.95 * RATE), 6000.0), (int(2.05 * RATE), 6000.0), (int(2.05 * RATE), 9000.0)]
    _a, _b, y = _apply(d, SpecSel(RATE, "log", (0,), [Shape("lasso", beside)]), action="erase")
    assert abs(_amp(y[mid], 3000) / 0.2 - 1) < 1e-3
    del fr


def test_take_away_and_channels(session):
    bed, tone = _sig()
    d = _doc(session, np.stack((bed + tone, bed + tone), axis=1))
    whole = Shape("rect", [(int(0.9 * RATE), 1000.0), (int(2.1 * RATE), 5000.0)])
    hole = Shape("rect", [(int(1.4 * RATE), 1000.0), (int(1.6 * RATE), 5000.0)], op="sub")
    sel = SpecSel(RATE, "log", (1,), [whole, hole])                      # the right channel only
    a, b, src = spectral.apply(d, sel, Params(action="erase", feather_ms=2.0, feather_hz=10.0))
    y = src.read(0, src.frames).astype(np.float64)
    x = d.read(a, b - a).astype(np.float64)
    assert np.array_equal(y[:, 0], x[:, 0])                             # the left: untouched
    t = lambda s: int(s * RATE) - a                                     # noqa: E731
    assert _amp(y[t(1.1):t(1.3), 1], 3000) < 1e-3 * 0.2               # erased …
    assert abs(_amp(y[t(1.45):t(1.55), 1], 3000) / 0.2 - 1) < 0.02     # … but not in the hole
    assert _amp(y[t(1.1):t(1.3), 1], 300) > 0.19                       # 300 Hz is under 1 kHz: kept


def test_fill_from_the_neighbours(session):
    """A burst over a noise bed: filled, the level there becomes the bed's around it (within 2 dB), the burst gone."""
    rng = np.random.default_rng(4)
    n = 3 * RATE
    bed = 0.05 * rng.standard_normal(n)
    t = np.arange(n) / RATE
    burst = 0.5 * np.sin(2 * np.pi * 1500 * t) * ((t >= 1.2) & (t < 1.5))
    d = _doc(session, bed + burst)
    sel = SpecSel(RATE, "lin", (0,), [Shape("rect", [(int(1.18 * RATE), 1200.0), (int(1.52 * RATE), 1800.0)])])
    _a, _b, y = _apply(d, sel, action="fill", feather_ms=5.0, feather_hz=20.0)
    mid = slice(int(1.25 * RATE), int(1.45 * RATE))
    assert _amp(y[mid], 1500) < 0.02
    from scipy import signal
    sos = signal.butter(4, [1300, 1700], btype="bandpass", fs=RATE, output="sos")
    band_y = np.sqrt(np.mean(signal.sosfilt(sos, y)[mid] ** 2))
    band_bed = np.sqrt(np.mean(signal.sosfilt(sos, bed)[mid] ** 2))
    assert abs(20 * math.log10(band_y / band_bed)) < 1.0, 20 * math.log10(band_y / band_bed)


def test_selection_only_preview(session):
    bed, tone = _sig()
    d = _doc(session, bed + tone)
    sel = SpecSel(RATE, "log", (0,), [Shape("rect", [(int(0.9 * RATE), 2500.0), (int(2.1 * RATE), 3600.0)])])
    a, b, src = spectral.apply(d, sel, Params(what="selection"), limit=(int(1.0 * RATE), int(1.5 * RATE)))
    assert (a, b) == (int(1.0 * RATE), int(1.5 * RATE))
    y = src.read(0, src.frames)[:, 0].astype(np.float64)
    assert abs(_amp(y[4800:-4800], 3000) / 0.2 - 1) < 0.01 and _amp(y[4800:-4800], 300) < 1e-4


def test_long_document_and_empty(session, monkeypatch):
    """A selection over a long stretch goes through block by block (the result as one source, the same length)."""
    sec = 40.0
    t = np.arange(int(sec * RATE)) / RATE
    x = 0.2 * np.sin(2 * np.pi * 300 * t) + 0.1 * np.sin(2 * np.pi * 5000 * t)
    d = _doc(session, x)
    sel = SpecSel(RATE, "log", (0,), [Shape("rect", [(RATE, 4000.0), (int(39 * RATE), 6500.0)])])
    a, b, src = spectral.apply(d, sel, Params(action="erase"))
    assert src.frames == b - a > 37 * RATE
    y = src.read(int(20 * RATE) - a, RATE)[:, 0].astype(np.float64)
    assert _amp(y, 5000) < 1e-4 and abs(_amp(y, 300) / 0.2 - 1) < 1e-3
    with pytest.raises(ValueError):
        spectral.apply(d, SpecSel(RATE, "log", (0,), []), Params())
    long = SpecSel(RATE, "log", (0,), [Shape("rect", [(0, 100.0), (int(39 * RATE), 200.0)])])
    monkeypatch.setattr(spectral, "FILL_MAX_S", 30.0)
    with pytest.raises(ValueError, match="fill_long"):
        spectral.apply(d, long, Params(action="fill"))


def test_one_undo_step(session):
    bed, tone = _sig()
    d = _doc(session, bed + tone)
    sel = SpecSel(RATE, "log", (0,), [Shape("rect", [(int(0.95 * RATE), 2500.0), (int(2.05 * RATE), 3600.0)])])
    a, b, src = spectral.apply(d, sel, Params(action="erase"))
    before = len(d.history)
    d.replace(a, b, src, "spectral", sel=None)
    assert len(d.history) == before + 1 and d.frames == len(bed)
    d.undo()
    assert np.allclose(d.read(0, d.frames)[:, 0], (bed + tone).astype(np.float32))


def test_span_and_masks():
    sel = SpecSel(RATE, "log", (0,), [Shape("rect", [(1000, 500.0), (5000, 1500.0)])])
    assert sel.span() == (1000, 5001)
    lo, hi = sel.freq_span()
    assert abs(lo - 500) < 1 and abs(hi - 1500) < 1
    times = np.arange(0, 6000, 100.0)
    freqs = np.array([100.0, 600.0, 1000.0, 2000.0])
    m = sel.mask(times, freqs)
    assert m[20, 1] == 1 and m[20, 0] == 0 and m[20, 3] == 0 and m[5, 1] == 0
    assert SpecSel(RATE, "log", (), [Shape("rect", [(0, 1.0), (1, 2.0)])]).empty()


def test_shapes_on_their_own_channels(session):
    bed, tone = _sig()
    d = _doc(session, np.stack((bed + tone, bed + tone), axis=1))
    left = Shape("rect", [(int(0.9 * RATE), 2500.0), (int(1.45 * RATE), 3600.0)], chs=(0,))
    right = Shape("rect", [(int(1.55 * RATE), 2500.0), (int(2.1 * RATE), 3600.0)], chs=(1,))
    sel = SpecSel(RATE, "log", (), [left, right])
    assert sel.all_channels() == (0, 1) and not sel.empty()
    a, _b, src = spectral.apply(d, sel, Params(action="erase", feather_ms=2.0))
    y = src.read(0, src.frames).astype(np.float64)
    t = lambda s: int(s * RATE) - a                                     # noqa: E731
    assert _amp(y[t(1.0):t(1.4), 0], 3000) < 1e-3 and abs(_amp(y[t(1.0):t(1.4), 1], 3000) / 0.2 - 1) < 1e-3
    assert _amp(y[t(1.6):t(2.0), 1], 3000) < 1e-3 and abs(_amp(y[t(1.6):t(2.0), 0], 3000) / 0.2 - 1) < 1e-3
