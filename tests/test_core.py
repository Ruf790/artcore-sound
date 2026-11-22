"""The engine without the window: sources and peak levels, the piece table with undo / redo, processing."""
import os

import numpy as np
import pytest

from sound import doc as D
from sound import proc, store


def brute_minmax(d, start, spp, n):
    out = np.full((n, d.channels, 2), np.nan, np.float32)
    for c in range(n):
        a = int(np.floor(start + c * spp))
        b = min(int(np.floor(start + (c + 1) * spp)), d.frames)
        if a >= d.frames or b <= a:
            continue
        y = d.read(a, b - a)
        out[c, :, 0] = y.min(0)
        out[c, :, 1] = y.max(0)
    return out


def make_doc(folder, x, rate=48000):
    x = np.asarray(x, np.float32)
    d = D.Document(rate=rate, channels=x.shape[1], folder=folder)
    d.set_initial(store.from_array(folder, x))
    return d


@pytest.fixture()
def noise(tmp_path):
    rng = np.random.default_rng(7)
    t = np.arange(1_234_567) / 48000
    x = np.stack([np.sin(2 * np.pi * 2 * t) * np.sin(2 * np.pi * 440 * t),
                  0.3 * rng.standard_normal(len(t))], 1).astype(np.float32)
    return x


def test_peak_levels_exact(tmp_path, noise):
    src = store.from_array(str(tmp_path), noise)
    assert src.frames == len(noise)
    # level 0 = min / max of every 256 frames, the next ones of 4 blocks each, the short tail included
    lv0 = src.levels[0]
    assert lv0.shape == (-(-len(noise) // 256), 2, 2)
    np.testing.assert_array_equal(lv0[10, :, 0], noise[2560:2816].min(0))
    np.testing.assert_array_equal(lv0[-1, :, 1], noise[(len(lv0) - 1) * 256:].max(0))
    for k in range(1, len(src.levels)):
        b = src.block(k)
        i = len(src.levels[k]) // 2
        np.testing.assert_array_equal(src.levels[k][i, :, 1], noise[i * b:(i + 1) * b].max(0))
    assert len(src.levels[-1]) <= store.MIN_BLOCKS * store.FACTOR


def test_writer_any_chunk_sizes(tmp_path, noise):
    w = store.SourceWriter(str(tmp_path), 2)
    pos = 0
    for n in (1, 255, 256, 257, 1000, 70000, 3):
        w.append(noise[pos:pos + n])
        pos += n
    w.append(noise[pos:])
    src = w.finish()
    np.testing.assert_array_equal(np.asarray(src.data), noise)
    ref = store.from_array(str(tmp_path), noise)
    for a, b in zip(src.levels, ref.levels):
        np.testing.assert_array_equal(np.asarray(a), np.asarray(b))


@pytest.mark.parametrize("spp", [1.0, 1.7, 37.0, 700.0, 1024.0, 1500.5, 9000.0, 123456.0])
def test_minmax_never_loses_a_peak(tmp_path, noise, spp):
    d = make_doc(str(tmp_path), noise)
    mm = d.minmax(777.0, spp, 600)
    br = brute_minmax(d, 777.0, spp, 600)
    ok = ~np.isnan(br[:, 0, 0])
    assert not np.isnan(mm[ok]).any()
    assert (mm[ok, :, 0] <= br[ok, :, 0] + 1e-6).all() and (mm[ok, :, 1] >= br[ok, :, 1] - 1e-6).all()
    # and close: on the smooth channel the envelope is within a few percent
    assert np.abs(mm[ok, 0] - br[ok, 0]).mean() < 0.05
    if spp < D.RAW_SPP:
        np.testing.assert_allclose(mm[ok], br[ok], atol=1e-6)
    # columns past the end are empty
    far = d.minmax(d.frames - 10 * spp, spp, 50)
    assert np.isnan(far[20:]).all()


def test_peak_is_exact(tmp_path, noise):
    d = make_doc(str(tmp_path), noise)
    np.testing.assert_array_equal(d.peak(), np.abs(noise).max(0))
    np.testing.assert_array_equal(d.peak(1000, 77777), np.abs(noise[1000:77777]).max(0))


def test_edits_against_a_numpy_model(tmp_path):
    """Random cuts, pastes, deletes, silences, trims and inserts: the document reads exactly like the same edits
    done on a plain array; then every undo and redo step reads like the array it was."""
    rng = np.random.default_rng(3)
    x = rng.standard_normal((50_000, 2)).astype(np.float32)
    d = make_doc(str(tmp_path), x)
    model = [x.copy()]
    clip_model = None
    clip = None
    for step in range(60):
        cur = model[-1]
        n = len(cur)
        a, b = sorted(rng.integers(0, max(1, n), 2))
        op = rng.choice(["cut", "copy", "paste", "delete", "silence", "insert", "trim", "replace"])
        if op in ("cut", "delete", "silence", "trim") and b <= a:
            continue
        if op == "cut":
            clip = d.cut(a, b)
            clip_model = cur[a:b].copy()
            new = np.concatenate((cur[:a], cur[b:]))
        elif op == "copy":
            if b <= a:
                continue
            clip = d.copy(a, b)
            clip_model = cur[a:b].copy()
            continue
        elif op == "paste":
            if clip is None:
                continue
            if rng.random() < 0.5:
                d.paste(clip, a)
                new = np.concatenate((cur[:a], clip_model, cur[a:]))
            else:
                d.paste(clip, a, b)
                new = np.concatenate((cur[:a], clip_model, cur[b:]))
        elif op == "delete":
            d.delete(a, b)
            new = np.concatenate((cur[:a], cur[b:]))
        elif op == "silence":
            d.silence(a, b)
            new = cur.copy()
            new[a:b] = 0
        elif op == "insert":
            k = int(rng.integers(1, 500))
            d.insert_silence(a, k)
            new = np.concatenate((cur[:a], np.zeros((k, 2), np.float32), cur[a:]))
        elif op == "trim":
            if (a, b) == (0, n):
                continue
            d.trim(a, b)
            new = cur[a:b].copy()
        else:                                           # an effect: a new source in place of [a, b)
            if b <= a:
                continue
            src = proc.apply_op(d, "gain", a, b, db=-6.0)
            d.replace(a, b, src, "gain")
            new = cur.copy()
            new[a:b] *= np.float32(10 ** (-6 / 20))
        model.append(new)
        assert d.frames == len(new)
        np.testing.assert_allclose(d.read(0, d.frames), new, atol=1e-6)
    assert len(d.history) == len(model)
    for i in range(len(model) - 1, -1, -1):              # undo all the way back …
        np.testing.assert_allclose(d.read(0, d.frames), model[i], atol=1e-6)
        if i:
            assert d.undo()
    assert not d.can_undo() and not d.dirty
    for i in range(1, len(model)):                     # … and redo all the way forward
        assert d.redo()
        np.testing.assert_allclose(d.read(0, d.frames), model[i], atol=1e-6)
    assert d.dirty and not d.can_redo()
    d.goto_state(3)
    np.testing.assert_allclose(d.read(0, d.frames), model[3], atol=1e-6)


def test_undo_restores_the_selection_and_costs_no_sound(tmp_path):
    x = np.ones((100_000, 2), np.float32)
    d = make_doc(str(tmp_path), x)
    files_before = set(os.listdir(tmp_path))
    d.sel = (1000, 5000)
    d.cursor = 1000
    for _ in range(200):                                 # 200 cut + paste steps write no sound to disk
        c = d.cut(1000, 5000)
        d.paste(c, 1000)
    assert set(os.listdir(tmp_path)) == files_before
    assert len(d.history) == 401
    d.sel = (1000, 5000)
    d.cursor = 1000
    d.delete(1000, 5000)
    assert d.sel is None and d.frames == 96_000
    d.undo()
    assert d.sel == (1000, 5000) and d.cursor == 1000 and d.frames == 100_000
    assert d.undo_label() == "paste" and d.redo_label() == "delete"


def test_new_history_branch_drops_redo(tmp_path):
    d = make_doc(str(tmp_path), np.ones((1000, 1), np.float32))
    d.delete(0, 100)
    d.delete(0, 100)
    d.undo()
    assert d.can_redo()
    d.silence(0, 10)
    assert not d.can_redo() and len(d.history) == 3


def test_processing_ops(tmp_path):
    t = np.arange(48000) / 48000
    x = np.stack([0.5 * np.sin(2 * np.pi * 100 * t), 0.25 * np.sin(2 * np.pi * 200 * t)], 1).astype(np.float32)
    d = make_doc(str(tmp_path), x)
    src = proc.apply_op(d, "normalize", 0, d.frames, db=-0.1)
    d.replace(0, d.frames, src, "normalize")
    assert abs(d.peak().max() - 10 ** (-0.1 / 20)) < 1e-5
    ratio = d.peak()[1] / d.peak()[0]
    assert abs(ratio - 0.5) < 1e-3                       # one gain for both channels
    for op, curve in (("fade_in", "linear"), ("fade_out", "scurve"), ("fade_in", "log"), ("fade_out", "exp")):
        y = d.read(0, d.frames)
        src = proc.apply_op(d, op, 1000, 11000, curve=curve)
        d.replace(1000, 11000, src, op)
        z = d.read(0, d.frames)
        g = proc.curve_gain(curve, np.linspace(0, 1, 10000))
        g = g if op == "fade_in" else g[::-1]
        np.testing.assert_allclose(z[1000:11000], y[1000:11000] * g[:, None].astype(np.float32), atol=1e-5)
        np.testing.assert_array_equal(z[:1000], y[:1000])
        np.testing.assert_array_equal(z[11000:], y[11000:])
    y = d.read(0, d.frames)
    d.replace(500, d.frames, proc.apply_op(d, "reverse", 500, d.frames), "reverse")
    np.testing.assert_array_equal(d.read(500, d.frames - 500), y[500:][::-1])
    d.replace(0, d.frames, proc.apply_op(d, "invert", 0, d.frames), "invert")
    np.testing.assert_array_equal(d.read(500, d.frames - 500), -y[500:][::-1])
    with pytest.raises(ValueError):
        d.silence(0, d.frames)
        proc.apply_op(d, "normalize", 0, d.frames)


def test_time_text():
    assert D.fmt_time(48000 * 3723.5, 48000) == "1:02:03.500"
    assert D.fmt_time(0, 44100) == "0:00:00.000"
    assert D.parse_time("1:02:03.5", 48000) == int(3723.5 * 48000)
    assert D.parse_time("62,5", 1000) == 62500
    assert D.parse_time("1234 smp", 48000) == 1234
    assert D.parse_time("abc", 48000) is None and D.parse_time("", 48000) is None


def test_stale_session_folders_are_removed(tmp_path, qapp):
    root = str(tmp_path / "root")
    os.makedirs(os.path.join(root, "s-deadbeef00"))
    with open(os.path.join(root, "s-deadbeef00", "x.f32"), "wb") as f:
        f.write(b"0" * 100)
    live = store.Session(root)
    assert not os.path.exists(os.path.join(root, "s-deadbeef00"))
    other = store.Session(root)                    # a second running copy keeps the first one's folder
    assert os.path.isdir(live.path) and os.path.isdir(other.path)
    other.close()
    live.close()
    assert os.listdir(root) == []
