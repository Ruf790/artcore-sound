"""Film tools (2.0): the shift, the retime (PAL ↔ film with and without the pitch), two points, the automatic sync
(a dub delayed and slowed is found), the channel matrix and the dialogue boost (whole = chunks, preview), the
downmixes (ITU / LoRo / Pro Logic II) and the upmix, channels out to mono files and back to 5.1, the batch steps,
the video window's follower and its still frames, the Film menu."""
import os
import subprocess

import numpy as np
import pytest
from conftest import pump

from sound import batch, chans, fx, media, store, sync
from sound.doc import Document

RATE = 48000


def _doc(session, x, rate=RATE, labels=None):
    x = np.asarray(x, np.float32)
    if x.ndim == 1:
        x = x[:, None]
    w = store.SourceWriter(session.folder(), x.shape[1])
    w.append(x)
    from sound import ff
    lay = ff.layout_for(x.shape[1])
    d = Document(rate=rate, channels=x.shape[1], folder=w.folder if hasattr(w, "folder") else session.folder(),
                 labels=tuple(labels or ff.labels(x.shape[1], lay)), layout=lay)
    d.set_initial(w.finish())
    return d


def _bursts(times, dur, rate=RATE, seed=2):
    r = np.random.default_rng(seed)
    x = np.zeros(int(dur * rate), np.float32)
    for t in times:
        n = int(rate * (0.03 + 0.1 * r.random()))
        s = int(t * rate)
        f = 200 + 3000 * r.random()
        if s < 0 or s + n > len(x):
            continue
        tt = np.arange(n) / rate
        x[s:s + n] += (0.3 * np.sin(2 * np.pi * f * tt) * np.exp(-tt * 20)).astype(np.float32)
    return x + 0.005 * np.random.default_rng(9).standard_normal(len(x)).astype(np.float32)


def test_shift(session):
    d = _doc(session, np.ones((RATE, 2), np.float32) * 0.5)
    d.add_marker(RATE // 2, "m")
    sync.shift(d, 4800)
    assert d.frames == RATE + 4800 and np.abs(d.read(0, 4800)).max() == 0 and d.markers[0].pos == RATE // 2 + 4800
    d.undo()
    sync.shift(d, -4800)
    assert d.frames == RATE - 4800 and d.markers[0].pos == RATE // 2 - 4800
    assert sync.video_frames_to_frames(25, 25.0, RATE) == RATE and sync.ms_to_frames(-10, RATE) == -480


@pytest.mark.parametrize("keep_pitch", [False, True])
def test_retime_pal(session, keep_pitch):
    tone = (0.5 * np.sin(2 * np.pi * 1000 * np.arange(RATE * 4) / RATE)).astype(np.float32)
    d = _doc(session, tone)
    d.add_marker(RATE * 2, "m")
    L = 25 / (24000 / 1001)
    src = sync.retime(d, L, keep_pitch, lead=2400)
    assert abs(src.frames - (RATE * 4 * L + 2400)) < RATE * 0.01
    sync.apply_retime(d, src, L, 2400, "speed")
    assert d.markers[0].pos == round(RATE * 2 * L + 2400) and d.can_undo()
    x = d.read(RATE, RATE)[:, 0]
    f = np.argmax(np.abs(np.fft.rfft(x * np.hanning(len(x))))) * RATE / len(x)
    want = 1000 if keep_pitch else 1000 / L                  # «like film»: the pitch goes down with the speed
    assert abs(f - want) < 3, f
    assert np.abs(d.read(0, 1500)).max() < 0.03              # the lead: silence first (rubberband smears a little)
    d.undo()
    assert d.frames == RATE * 4


def test_retime_filter_precision():
    for _n, a, b in sync.SPEEDS:
        s, r2 = sync.rate_pair(a / b, RATE)
        assert abs(r2 / s - a / b) < 1e-12 and s >= RATE // 2
    s, r2 = sync.rate_pair(1.000293, RATE)
    assert abs(r2 / s - 1.000293) < 1e-9


def test_two_points():
    L, lead = sync.two_points(RATE, 11 * RATE, 1.5, 11.9, RATE)
    assert abs(L - 1.04) < 1e-9 and lead == round(1.5 * RATE - RATE * L)
    with pytest.raises(ValueError):
        sync.two_points(RATE, RATE, 1, 2, RATE)


def test_auto_sync_finds_offset_and_speed(session):
    rng = np.random.default_rng(1)
    dur = 240.0
    tau = np.sort(rng.random(700) * dur)
    L, off = 25 / (24000 / 1001), 1.234
    ref = _doc(session, _bursts(tau, dur))
    dub = _doc(session, _bursts((tau - off) / L, dur / L))
    a, b = sync.envelope_of_doc(dub), sync.envelope_of_doc(ref)
    est = sync.estimate(a, b)
    assert abs(est.L - L) < 1e-6 and abs(est.offset - off) < 0.012, (est.L, est.offset)
    assert est.confidence > 0.7 and est.agree >= est.windows - 1 >= 2
    # same speed: the 1.0 candidate stays
    dub2 = _doc(session, _bursts(tau - 0.5, dur))
    est2 = sync.estimate(sync.envelope_of_doc(dub2), b)
    assert est2.L == 1.0 and abs(est2.offset - 0.5) < 0.012
    # unrelated sound: low confidence
    other = _doc(session, _bursts(np.sort(rng.random(700) * dur), dur, seed=5))
    est3 = sync.estimate(sync.envelope_of_doc(other), b)
    assert est3.confidence < 0.35
    # applying it puts the bursts where the reference has them
    src = sync.retime(dub, est.L, False, est.lead(RATE))
    sync.apply_retime(dub, src, est.L, est.lead(RATE), "auto")
    again = sync.estimate(sync.envelope_of_doc(dub), b)
    assert again.L == 1.0 and abs(again.offset) < 0.012 and again.confidence > 0.7


def test_envelope_chunks_equal(session):
    x = _bursts(np.arange(0.5, 20, 0.7), 21)
    e1 = sync.Envelope(8000)
    from scipy.signal import resample_poly
    y = resample_poly(x, 1, 6).astype(np.float32)
    e1.feed(y)
    e2 = sync.Envelope(8000)
    for i in range(0, len(y), 777):
        e2.feed(y[i:i + 777])
    assert np.allclose(e1.result(), e2.result(), atol=1e-5)


def test_envelope_of_film_track(session, media_dir):
    path = os.path.join(media_dir, "Фильм (2024).mkv")
    e = sync.envelope_of_file(path, 1, seconds=3)
    assert abs(len(e) - 300) < 3


def _six(session, n=RATE):
    t = np.arange(n) / RATE
    x = np.stack([0.1 * (k + 1) * np.sin(2 * np.pi * (200 + 100 * k) * t) for k in range(6)], axis=1)
    return _doc(session, x, labels=("L", "R", "C", "LFE", "Ls", "Rs"))


def test_matrix_swap_and_chunks(session):
    d = _six(session)
    eff = fx.get("chmatrix")
    v = eff.clean(eff.presets["cm_p_swap_c_lfe"])
    src = fx.render(d, 0, d.frames, eff, v)
    y, x = src.read(0, d.frames), d.read(0, d.frames)
    assert np.allclose(y[:, 2], x[:, 3], atol=1e-6) and np.allclose(y[:, 3], x[:, 2], atol=1e-6)
    assert np.allclose(y[:, [0, 1, 4, 5]], x[:, [0, 1, 4, 5]], atol=1e-6)
    v2 = dict(v, m00=-6.0, m01=-6.0, normalize=False)              # L out = L and R at −6 dB
    p = eff.processor(RATE, d.labels, v2)
    whole = p.process(x)
    p2 = eff.processor(RATE, d.labels, v2)
    parts = np.concatenate([p2.process(x[i:i + 1000]) for i in range(0, len(x), 1000)])
    assert np.allclose(whole, parts) and np.allclose(whole[:, 0], 0.5012 * (x[:, 0] + x[:, 1]), atol=1e-3)


def test_dialog_boost(session):
    d = _six(session)
    eff = fx.get("dialog")
    v = eff.clean({"boost": 6.0, "others": -6.0})
    y = fx.render(d, 0, d.frames, eff, v).read(0, d.frames)
    x = d.read(0, d.frames)
    g = lambda a, b: 20 * np.log10(np.abs(a).max() / np.abs(b).max())    # noqa: E731
    assert abs(g(y[:, 2], x[:, 2]) - 6) < 0.05 and abs(g(y[:, 0], x[:, 0]) + 6) < 0.05
    assert abs(g(y[:, 3], x[:, 3])) < 0.01                                 # the LFE stays
    # stereo: a 1 kHz tone in the middle goes up, the same tone only in the sides goes down
    t = np.arange(RATE) / RATE
    tone = 0.2 * np.sin(2 * np.pi * 1000 * t)
    st = _doc(session, np.stack((tone, tone), axis=1))
    sy = fx.render(st, 0, st.frames, eff, v).read(RATE // 2, RATE // 2)
    assert abs(20 * np.log10(np.abs(sy[:, 0]).max() / 0.2) - 6) < 0.3
    side = _doc(session, np.stack((tone, -tone), axis=1))
    sd = fx.render(side, 0, side.frames, eff, v).read(RATE // 2, RATE // 2)
    assert abs(20 * np.log10(np.abs(sd[:, 0]).max() / 0.2) + 6) < 0.3


@pytest.mark.parametrize("kind", chans.DOWNMIX)
def test_downmix(session, kind):
    n = RATE
    t = np.arange(n) / RATE
    x = np.zeros((n, 6), np.float32)
    x[:, 2] = 0.5 * np.sin(2 * np.pi * 500 * t)               # only the centre
    d = _doc(session, x, labels=("L", "R", "C", "LFE", "Ls", "Rs"))
    src = chans.convert(d, 2, kind)
    assert src.frames == n
    y = src.read(0, n)
    assert np.allclose(y[:, 0], y[:, 1], atol=1e-5) and np.abs(y).max() <= 1.0
    if kind == "pl2":
        # a surround-only signal comes out of phase in Lt / Rt (it decodes to the back)
        x2 = np.zeros((n, 6), np.float32)
        x2[:, 4] = x2[:, 5] = 0.5 * np.sin(2 * np.pi * 500 * t)
        y2 = chans.convert(_doc(session, x2, labels=("L", "R", "C", "LFE", "Ls", "Rs")), 2, "pl2").read(0, n)
        mid = slice(n // 4, 3 * n // 4)
        corr = np.corrcoef(y2[mid, 0], y2[mid, 1])[0, 1]
        assert corr < -0.9 and src.frames == n
        # whole = chunks
        dm1, dm2 = chans.Downmix("pl2", d.labels), chans.Downmix("pl2", d.labels)
        a = dm1.process(x2)
        b = np.concatenate([dm2.process(x2[i:i + 3000]) for i in range(0, n, 3000)])
        assert np.allclose(a, b, atol=1e-6)


def test_upmix(session):
    t = np.arange(RATE) / RATE
    low = 0.3 * np.sin(2 * np.pi * 50 * t)
    mid = 0.3 * np.sin(2 * np.pi * 1000 * t)
    st = _doc(session, np.stack((low + mid, low - mid), axis=1))      # 50 Hz in the middle, 1 kHz in the sides
    y = chans.convert(st, 6, "upmix").read(RATE // 2, RATE // 2)
    assert y.shape[1] == 6
    rms = np.sqrt((y ** 2).mean(axis=0))
    assert rms[3] > 0.1 and rms[2] < rms[3]                     # the LFE takes the 50 Hz
    assert rms[4] > 0.1 and rms[5] > 0.1                        # the surrounds take the difference


def test_extract_and_assemble(session, tmp_path):
    d = _six(session)
    paths = []
    for k, lab in enumerate(d.labels):
        one = Document(rate=RATE, channels=1, folder=d.folder, labels=("M",), layout="mono")
        one.set_initial(chans.extract(d, k))
        p = str(tmp_path / f"Фильм_{lab}.wav")
        media.save(one, p, "wav", "32f", markers=False)
        paths.append(p)
    import random
    shuffled = paths[:]
    random.Random(3).shuffle(shuffled)
    assert chans.guess_slots(shuffled) == paths
    src, rate = chans.assemble(paths, session.folder())
    assert rate == RATE and src.frames == d.frames
    assert np.allclose(src.read(0, d.frames), d.read(0, d.frames), atol=1e-6)


def test_batch_steps(session):
    d = _six(session)
    step = batch.Step("channels", {"channels": 2, "method": "pl2"})
    assert "Pro Logic" in step.summary()
    batch.apply_step(d, step, media.Progress())
    assert d.channels == 2 and d.frames == RATE
    d2 = _six(session)
    batch.apply_step(d2, batch.Step("chmatrix", fx.get("chmatrix").presets["cm_p_mute_lfe"]), media.Progress())
    assert np.abs(d2.read(0, RATE)[:, 3]).max() == 0
    batch.apply_step(d2, batch.Step("dialog", {"boost": 3.0}), media.Progress())
    assert batch.Step("dialog").title()


def test_follower():
    f = sync_f = __import__("sound.videoui", fromlist=["Follower"]).Follower(25.0)
    assert f.step(1.0, False, None, True) == [("seek", 1.0)]
    assert f.step(1.01, False, 1.0, True) == []                      # within half a frame: no new seek
    assert f.step(1.2, False, 1.0, True) == [("seek", 1.2)]
    assert f.step(2.0, True, 1.2, True) == [("seek", 2.0), ("play", 1.0)]
    assert f.step(2.5, True, 2.5, False) == []
    acts = f.step(3.0, True, 3.03, False)                            # 30 ms ahead: slower
    assert acts and acts[0][0] == "play" and acts[0][1] < 1.0
    assert f.step(3.5, True, 3.5, False) == [("play", 1.0)]
    assert f.step(4.0, True, 4.5, False)[0] == ("seek", 4.0)         # a jump: seek
    assert f.step(4.0, False, 4.0, False) == [("pause",), ("seek", 4.0)]   # stopped: the exact frame
    del sync_f


def test_still_frames_and_window(qapp, session, data_dir, media_dir, monkeypatch):
    monkeypatch.setenv("ARTCORE_NO_MPV", "1")
    from sound import videoui
    videoui._mpv, videoui._mpv_error = None, ""
    png = videoui.grab_frame(os.path.join(media_dir, "Фильм (2024).mkv"), 1.0)
    assert png[:4] == b"\x89PNG"
    from test_volume import _win
    w = _win(qapp, session)
    d = media.open_file(os.path.join(media_dir, "Фильм (2024).mkv"), session.folder(), 0)
    w.add_doc(d)
    d.cursor = int(1.5 * d.rate)
    w.a_video.trigger()
    vw = w.video_win
    assert vw is not None and vw.engine == "frames" and w.a_video.isChecked()
    assert pump(qapp, 10, lambda: vw.pic.frames >= 1)
    assert abs(vw.pic.pos - 1.5) < 1e-6 and "FFmpeg" in vw.info.text()
    w.a_video.trigger()
    pump(qapp, 0.1)
    assert w.video_win is None and not w.a_video.isChecked()
    for x in w.docs:
        x.mark_saved()
    w.close()
    videoui._mpv, videoui._mpv_error = None, ""


def test_film_menu_dialogs(qapp, session, data_dir, media_dir, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from test_volume import _win

    from sound import filmui
    w = _win(qapp, session)
    d = media.open_file(os.path.join(media_dir, "Фильм (2024).mkv"), session.folder(), 0)
    w.add_doc(d)
    pump(qapp, 0.1)
    for a in w.film_actions:
        assert a.isEnabled()
    dlg = filmui.AutoSyncDialog(w, d)
    assert dlg.ref.count() == 1 and "eng" in dlg.ref.itemText(0)
    dlg.speeds.setChecked(False)
    dlg.find()
    assert pump(qapp, 30, lambda: dlg._run is None)
    assert dlg.result_est is not None and dlg.b_apply.isEnabled() and "мс" in dlg.res.text()
    dlg.reject()
    ex = filmui.ExtractDialog(d, w.cfg)
    assert len(ex.jobs()) == 6 and ex.jobs()[2][1].endswith("_C.wav")
    ex.reject()
    asm = filmui.AssembleDialog(w.cfg)
    assert not asm.box.button(asm.box.StandardButton.Ok).isEnabled()
    asm.reject()
    # Process → Channels offers the matrices for 5.1 → stereo
    shown = {}

    def fake_exec(self):
        combos = self.findChildren(__import__("PySide6.QtWidgets", fromlist=["QComboBox"]).QComboBox)
        shown["methods"] = [combos[1].itemData(i) for i in range(combos[1].count())]
        return QDialog.Rejected
    from sound.dialogs import ValueDialog
    monkeypatch.setattr(ValueDialog, "exec", fake_exec)
    w.channels_dialog()
    assert shown["methods"] == list(chans.DOWNMIX)
    for x in w.docs:
        x.mark_saved()
    w.close()
