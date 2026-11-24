"""3.0 — the multitrack's core: the project's media (WAV read in place, other formats decoded once into the cache), the
model and its history, the mixer (checked frame for frame against numpy), plug-in delay compensation, .acproj and the
crash journal, mixdown / stems / regions / bounce, recording's placement. Real files made by ffmpeg."""
import json
import os
import subprocess

import numpy as np
import pytest

from conftest import ff

RATE = 48000


@pytest.fixture(scope="module")
def mt_media(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("mtmedia"))
    tone = "aevalsrc='0.5*sin(2*PI*440*t)|0.3*sin(2*PI*660*t)':s=48000:d=2"
    for name, codec in (("s16", "pcm_s16le"), ("s24", "pcm_s24le"), ("s32", "pcm_s32le"), ("f32", "pcm_f32le")):
        ff("-f", "lavfi", "-i", tone, "-c:a", codec, os.path.join(d, name + ".wav"))
    ff("-f", "lavfi", "-i", "sine=f=330:d=2:r=48000", "-c:a", "pcm_s16le", os.path.join(d, "mono.wav"))
    ff("-f", "lavfi", "-i", "sine=f=440:d=2:r=44100", "-c:a", "flac", os.path.join(d, "other_rate.flac"))
    ff("-f", "lavfi", "-i", "sine=f=440:d=2:r=48000", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "192k",
       os.path.join(d, "song.mp3"))
    ff("-f", "lavfi", "-i", "sine=f=440:d=2:r=48000", "-filter_complex",
       "[0]pan=5.1|c0=c0|c1=0.5*c0|c2=0.7*c0|c3=0.3*c0|c4=0.25*c0|c5=0.2*c0", "-c:a", "pcm_f32le",
       os.path.join(d, "six.wav"))
    return d


def ref(path, rate=RATE):
    ch = int(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "stream=channels", "-of",
                                      "csv=p=0", path]).split()[0])
    raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", path, "-ar", str(rate), "-f", "f32le", "-"])
    return np.frombuffer(raw, np.float32).reshape(-1, ch)


def load(path, cache, rate=RATE):
    from sound.mt import media as M
    m = M.load(M.Media(M.new_media_id(), path), rate, cache)
    assert not m.offline, m.error
    return m


def project(tmp_path, channels=2):
    from sound.mt.model import Project
    return Project(RATE, channels, "T", str(tmp_path / "T.acproj"), str(tmp_path))


def mix(p, a=0, b=None):
    from sound.mt import engine
    out = []
    engine.render(p, a, p.frames if b is None else b, out.append)
    return np.concatenate(out) if out else np.zeros((0, p.channels), np.float32)


def live(p, sizes=(1024, 777, 2048, 333)):
    """The same mix as the player gets it: read in small blocks of changing sizes."""
    from sound.mt import engine
    e = engine.Engine(p, live=True)
    parts, pos, i = [], 0, 0
    try:
        while pos < p.frames:
            k = min(sizes[i % len(sizes)], p.frames - pos)
            parts.append(e.read(pos, k))
            pos += k
            i += 1
    finally:
        e.close()
    return np.concatenate(parts)


# ── media ─────────────────────────────────────────────────────
def test_wav_read_in_place_bit_exact(mt_media, tmp_path):
    cache = str(tmp_path / "cache")
    os.makedirs(cache)
    for name in ("s16", "s24", "s32", "f32", "six"):
        path = os.path.join(mt_media, name + ".wav")
        m = load(path, cache)
        assert m.cached == ""                                  # no copy: the file itself
        x = m.src.read(0, m.frames)
        assert np.array_equal(x, ref(path)), name
        # the peaks are exact: the largest sample
        from sound import store
        pk = store.source_minmax(m.src, 0, m.frames, 2)
        assert max(float(a[..., 1].max()) for *_r, a in pk) == float(x.max())
        assert m.src.read(m.frames - 10, 30)[10:].max() == 0  # past the end: silence


def test_other_formats_decoded_once_into_the_cache(mt_media, tmp_path):
    cache = str(tmp_path / "cache")
    os.makedirs(cache)
    for name in ("song.mp3", "other_rate.flac"):
        m = load(os.path.join(mt_media, name), cache)
        assert m.cached and os.path.isfile(m.cached) and m.rate == RATE
        r = ref(os.path.join(mt_media, name))
        assert abs(m.frames - len(r)) < 64
        before = set(os.listdir(cache))
        m2 = load(os.path.join(mt_media, name), cache)       # again: found in the cache, nothing new written
        assert m2.cached == m.cached and set(os.listdir(cache)) == before
        assert np.array_equal(m2.src.read(0, m2.frames), m.src.read(0, m.frames))


def test_missing_file_is_offline(tmp_path):
    from sound.mt import media as M
    m = M.load(M.Media("x", str(tmp_path / "gone.wav")), RATE, str(tmp_path))
    assert m.offline and m.error


def test_wav_writer_riff_and_rf64(tmp_path):
    from sound.mt import media as M
    x = (np.random.default_rng(1).standard_normal((50000, 6)) * 0.2).astype(np.float32)
    w = M.WavWriter(str(tmp_path / "a.wav"), 6, RATE, str(tmp_path / "a.pk"))
    w.append(x[:12345])
    w.append(x[12345:])
    src = w.finish()
    assert np.array_equal(src.read(0, 50000), x)
    assert np.array_equal(ref(str(tmp_path / "a.wav")), x)                 # ffmpeg reads it too
    # the RF64 header (written past 4 GB) is read the same
    w2 = M.WavWriter(str(tmp_path / "b.wav"), 2, RATE, str(tmp_path / "b.pk"))
    w2.append(x[:, :2])
    w2._f.seek(0)
    w2._f.write(w2._header(50000, True))
    w2._f.seek(0, 2)
    w2.frames = 50000
    w2._f.close()
    os.replace(str(tmp_path / "b.wav.part"), str(tmp_path / "b.wav"))
    info = M.parse_wav(str(tmp_path / "b.wav"))
    assert info.frames == 50000 and open(str(tmp_path / "b.wav"), "rb").read(4) == b"RF64"
    assert np.array_equal(M.PcmSource(str(tmp_path / "b.wav"), info).read(0, 50000), x[:, :2])


# ── the model ─────────────────────────────────────────────────
def test_model_history_and_clip_edits(tmp_path):
    from sound.mt.model import MASTER
    p = project(tmp_path)
    tr = p.add_track("A")
    c = p.add_clip(tr.id, "m", 1000, 48000)
    p.set_track(tr.id, gain_db=-1, merge="g")
    p.set_track(tr.id, gain_db=-2, merge="g")                   # a fader drag: one step
    assert [s.label for s in p.history][-2:] == ["add clip", "track"]
    right = p.split_clips(20000)
    assert len(p.track(tr.id).clips) == 2 and p.clip(right[0])[1].offset == 19000
    p.trim_clip(c.id, "left", 5000)
    left = p.clip(c.id)[1]
    assert (left.start, left.offset, left.length) == (5000, 4000, 15000)
    p.move_clips({c.id}, 100000)
    assert p.clip(c.id)[1].start == 105000
    for _ in range(3):
        p.undo()
    assert p.clip(c.id)[1].start == 1000 and len(p.track(tr.id).clips) == 1
    p.redo()
    assert len(p.track(tr.id).clips) == 2
    assert p.can_undo() and p.dirty
    # routing: a bus may not feed back into what feeds it
    b1 = p.add_track("B1", "bus")
    b2 = p.add_track("B2", "bus")
    p.set_track(b1.id, output=b2.id)
    p.set_track(b2.id, output=b1.id)                              # a loop: refused
    assert p.track(b2.id).output == MASTER
    p.set_send(tr.id, b1.id, db=-6)
    p.remove_tracks([b1.id])
    assert not p.track(tr.id).sends


def test_overlap_gets_a_crossfade_and_range_delete_ripples(tmp_path):
    p = project(tmp_path)
    tr = p.add_track("A")
    a = p.add_clip(tr.id, "m", 0, 10000)
    b = p.add_clip(tr.id, "m", 8000, 10000)
    assert p.clip(b.id)[1].fade_in == 2000                         # laid over a's end
    p.add_region(15000, 17000, "R")
    p.delete_range(1000, 3000, ripple=True)
    clips = sorted(p.track(tr.id).clips, key=lambda c: c.start)
    assert [(c.start, c.length) for c in clips] == [(0, 1000), (1000, 7000), (6000, 10000)]
    assert p.markers[0].pos == 13000
    clip = p.copy_range(500, 1500)
    assert sorted(c.length for _i, c in clip["clips"]) == [500, 500]
    del a


def test_takes(tmp_path):
    from sound.mt.model import Take
    p = project(tmp_path)
    tr = p.add_track("A")
    c = p.add_clip(tr.id, "m1", 0, 1000)
    p.add_take(c.id, Take("m2", 50))
    c2 = p.clip(c.id)[1]
    assert c2.media == "m2" and c2.offset == 50 and len(c2.takes) == 2
    p.set_clip(c.id, take=0)
    assert p.clip(c.id)[1].media == "m1"
    p.delete_take(c.id, 0)
    assert p.clip(c.id)[1].media == "m2"


# ── the mixer ─────────────────────────────────────────────────
def test_mix_is_bit_exact_live_and_offline(mt_media, tmp_path):
    cache = str(tmp_path / "c")
    os.makedirs(cache)
    p = project(tmp_path)
    m1 = load(os.path.join(mt_media, "s16.wav"), cache)
    m2 = load(os.path.join(mt_media, "mono.wav"), cache)
    p.media[m1.id], p.media[m2.id] = m1, m2
    t1, t2 = p.add_track("A"), p.add_track("B", channels=1)
    p.add_clip(t1.id, m1.id, 1000, 50000)
    p.add_clip(t2.id, m2.id, 20000, 40000, offset=500)
    a = m1.src.read(0, 96000).astype(np.float64)
    b = m2.src.read(0, 96000).astype(np.float64)
    want = np.zeros((p.frames, 2))
    want[1000:51000] += a[:50000]
    th = np.pi / 4                                                 # a mono track in the middle: −3 dB each side
    want[20000:60000, 0] += b[500:40500, 0] * np.cos(th)
    want[20000:60000, 1] += b[500:40500, 0] * np.sin(th)
    y = mix(p)
    assert np.array_equal(y, want.astype(np.float32))
    assert np.array_equal(live(p), y)                              # what is heard is what is written
    # fader, pan (balance of a stereo track), polarity, mute, solo
    p.set_track(t1.id, gain_db=-6.0, pan=0.5, phase=True)
    g = 10 ** (-6 / 20)
    want2 = np.zeros((p.frames, 2))
    want2[1000:51000, 0] -= a[:50000, 0] * g * 0.5
    want2[1000:51000, 1] -= a[:50000, 1] * g
    want2[20000:60000, 0] += b[500:40500, 0] * np.cos(th)
    want2[20000:60000, 1] += b[500:40500, 0] * np.sin(th)
    assert np.allclose(mix(p), want2, atol=1e-6)
    p.set_track(t2.id, solo=True)
    y = mix(p)
    assert not y[1000:19000].any() and y[20000:60000].any()
    p.set_track(t2.id, solo=False, mute=True)
    assert not mix(p)[51000:].any()


def test_crossfade_fades_envelope(mt_media, tmp_path):
    cache = str(tmp_path / "c")
    os.makedirs(cache)
    p = project(tmp_path)
    m = load(os.path.join(mt_media, "f32.wav"), cache)
    p.media[m.id] = m
    tr = p.add_track("A")
    p.add_clip(tr.id, m.id, 0, 20000)
    p.add_clip(tr.id, m.id, 16000, 20000, offset=40000)             # a 4000-frame linear crossfade
    x = m.src.read(0, 96000).astype(np.float64)
    y = mix(p).astype(np.float64)
    k = np.arange(4000) / 4000
    want = x[16000:20000] * (1 - k)[:, None] + x[40000:44000] * k[:, None]
    assert np.allclose(y[16000:20000], want, atol=1e-6)
    assert np.allclose(y[:16000], x[:16000], atol=1e-7) and np.allclose(y[20000:36000], x[44000:60000], atol=1e-7)
    # a volume envelope: 0 dB → −12 dB, linear in dB between its points
    p.set_envelope(tr.id, "vol", [(0, 0.0), (36000, -12.0)])
    y2 = mix(p).astype(np.float64)
    i = 30000
    g = 10 ** ((-12.0 * i / 36000) / 20)
    assert abs(y2[i, 0] - y[i, 0] * g) < 1e-6
    assert np.array_equal(live(p), mix(p))


def test_bus_send_and_master(mt_media, tmp_path):
    cache = str(tmp_path / "c")
    os.makedirs(cache)
    p = project(tmp_path)
    m = load(os.path.join(mt_media, "s16.wav"), cache)
    p.media[m.id] = m
    tr = p.add_track("A")
    p.add_clip(tr.id, m.id, 0, 30000)
    bus = p.add_track("Bus", "bus")
    p.set_track(tr.id, output=bus.id)
    p.set_track(bus.id, gain_db=-6.0)
    x = m.src.read(0, 30000).astype(np.float64)
    g = 10 ** (-6 / 20)
    assert np.allclose(mix(p), x * g, atol=1e-6)
    p.set_track(tr.id, output=0)
    p.set_send(tr.id, bus.id, db=-6.0)                              # a post-fader send: dry + half
    assert np.allclose(mix(p), x * (1 + g * g), atol=1e-6)
    p.set_track(p.master.id, gain_db=-6.0)
    assert np.allclose(mix(p), x * (1 + g * g) * g, atol=1e-6)


def test_six_channel_project(mt_media, tmp_path):
    cache = str(tmp_path / "c")
    os.makedirs(cache)
    p = project(tmp_path, 6)
    six = load(os.path.join(mt_media, "six.wav"), cache)
    st = load(os.path.join(mt_media, "s16.wav"), cache)
    p.media[six.id], p.media[st.id] = six, st
    t6, t2 = p.add_track("6", channels=6), p.add_track("2")
    p.add_clip(t6.id, six.id, 0, 20000)
    p.add_clip(t2.id, st.id, 0, 20000)
    y = mix(p).astype(np.float64)
    x6 = six.src.read(0, 20000).astype(np.float64)
    x2 = st.src.read(0, 20000).astype(np.float64)
    assert np.allclose(y[:, 2:], x6[:, 2:], atol=1e-7)              # C, LFE, Ls, Rs: channel to channel
    assert np.allclose(y[:, :2], x6[:, :2] + x2, atol=1e-6)         # the stereo track on L / R


class _DelayFx:
    """A test effect: the sound `latency` frames late (as a look-ahead plug-in reports it)."""


def _install_delay_fx(n: int):
    from sound import fx
    from sound.fx.base import Effect, Fifo, Processor

    class P(Processor):
        latency = n

        def configure(self, old):
            self.f = Fifo(n, self.ch)

        def _process(self, x):
            return self.f(x)

    class E(Effect):
        key = "t_delay"
        name = "fx_delay"
        params = []
        proc_class = P
    fx.EFFECTS["t_delay"] = E()
    return "t_delay"


def test_plugin_delay_compensation(tmp_path):
    from sound import fx
    from sound.mt import media as M
    from sound.mt.model import Slot
    key = _install_delay_fx(1500)
    try:
        cache = str(tmp_path / "c")
        os.makedirs(cache)
        x = np.zeros((20000, 2), np.float32)
        x[5000] = 0.5                                              # an impulse
        w = M.WavWriter(str(tmp_path / "imp.wav"), 2, RATE, str(tmp_path / "imp.pk"))
        w.append(x)
        w.finish()
        m = load(str(tmp_path / "imp.wav"), cache)
        p = project(tmp_path)
        p.media[m.id] = m
        a, b = p.add_track("dry"), p.add_track("delayed")
        p.add_clip(a.id, m.id, 0, 20000)
        p.add_clip(b.id, m.id, 0, 20000)
        p.set_fx(b.id, [Slot(key, {})])
        y = mix(p)
        assert np.flatnonzero(y[:, 0]).tolist() == [5000]           # both impulses at the same frame
        assert y[5000, 0] == np.float32(1.0)
        bus = p.add_track("bus", "bus")
        p.set_fx(bus.id, [Slot(key, {})])
        p.set_send(a.id, bus.id, db=0.0)
        y = mix(p)
        assert np.flatnonzero(y[:, 0]).tolist() == [5000]           # through a bus with latency too
        assert np.array_equal(live(p), y)
    finally:
        fx.EFFECTS.pop(key, None)


# ── files ─────────────────────────────────────────────────────
def test_project_file_and_journal(mt_media, tmp_path):
    import shutil
    from sound.mt import projfile
    from sound.mt.model import Slot
    cache = str(tmp_path / "c")
    os.makedirs(cache)
    folder = tmp_path / "Proj"
    folder.mkdir()
    shutil.copy(os.path.join(mt_media, "s16.wav"), folder / "s16.wav")
    p = project(folder)
    p.path = str(folder / "Proj.acproj")
    m = load(str(folder / "s16.wav"), cache)
    p.media[m.id] = m
    tr = p.add_track("Голос")
    bus = p.add_track("Bus", "bus")
    p.add_clip(tr.id, m.id, 1000, 40000)
    p.set_send(tr.id, bus.id, db=-3, pre=True)
    p.set_fx(tr.id, [Slot("eq", {}), Slot("comp", {}, bypass=True)])
    p.set_envelope(tr.id, "pan", [(0, -1), (20000, 1)])
    p.add_region(0, 9000, "Куплет")
    projfile.save(p)
    data = json.load(open(p.path, encoding="utf-8"))
    assert data["media"][0]["path"] == "s16.wav"                    # relative: the folder can move
    y = mix(p)
    moved = tmp_path / "Moved"
    shutil.copytree(folder, moved)
    shutil.rmtree(folder)
    q = projfile.load(str(moved / "Proj.acproj"))
    assert projfile.load_media(q, cache) == []
    assert q.tracks == p.tracks and q.markers[0].name == "Куплет" and not q.dirty
    assert np.array_equal(mix(q), y)
    # the journal: the whole history comes back
    q.set_track(tr.id, gain_db=-10)
    q.split_clips(15000)
    q.journal_folder = str(tmp_path)
    projfile.write_journal(q)
    r = projfile.from_journal(json.load(open(tmp_path / "project.json", encoding="utf-8")))
    projfile.load_media(r, cache)
    assert len(r.history) == len(q.history) and r.tracks == q.tracks and r.dirty
    r.undo()
    r.undo()
    assert r.tracks == p.tracks


def test_mixdown_stems_regions_bounce(mt_media, tmp_path):
    from sound.mt import render
    cache = str(tmp_path / "c")
    os.makedirs(cache)
    p = project(tmp_path)
    p.name = "Song"
    m1 = load(os.path.join(mt_media, "s16.wav"), cache)
    m2 = load(os.path.join(mt_media, "mono.wav"), cache)
    p.media[m1.id], p.media[m2.id] = m1, m2
    t1, t2 = p.add_track("A"), p.add_track("B", channels=1)
    p.add_clip(t1.id, m1.id, 0, 30000)
    p.add_clip(t2.id, m2.id, 10000, 30000)
    p.add_region(0, 20000, "One")
    p.add_region(20000, 40000, "Two")
    render.mixdown(p, str(tmp_path / "mix.wav"), "wav", "32f")
    assert np.array_equal(ref(str(tmp_path / "mix.wav")), mix(p))
    stems = render.stems(p, str(tmp_path / "stems"), "wav", "32f", [t1.id, t2.id])
    s1, s2 = ref(stems[0]), ref(stems[1])
    assert np.allclose(s1 + s2, mix(p), atol=1e-6)                   # the stems add up to the mix
    regs = render.regions(p, str(tmp_path / "regs"), "flac", "24")
    assert [os.path.basename(x) for x in regs] == ["01 One.flac", "02 Two.flac"]
    assert len(ref(regs[1])) == 20000
    d = render.to_document(p, str(tmp_path), 0, None)
    assert d.frames == p.frames and np.array_equal(d.read(0, d.frames), mix(p)) and len(d.markers) == 2
    src = render.bounce_track(p, t2.id, str(tmp_path / "b.wav"), cache)
    assert src.frames == 40000 and np.array_equal(src.read(10000, 30000)[:, 0], m2.src.read(0, 30000)[:, 0])


# ── recording ─────────────────────────────────────────────────
def test_record_placement_punch_and_takes(tmp_path):
    from sound.mt.rec import place
    p = project(tmp_path)
    tr = p.add_track("A")
    ids = place(p, [(tr.id, "r1", 1000, 50000)])                     # plain: where it began
    c = p.clip(ids[0])[1]
    assert (c.start, c.offset, c.length) == (1000, 0, 50000)
    p.undo()
    place(p, [(tr.id, "r2", 1000, 50000)], punch=(11000, 21000))     # punch: only the selection
    c = p.track(tr.id).clips[-1]
    assert (c.start, c.offset, c.length) == (11000, 10000, 10000)
    place(p, [(tr.id, "r3", 5000, 30000)], punch=(11000, 21000))     # the same span again: a take
    c = p.track(tr.id).clips[-1]
    assert len(p.track(tr.id).clips) == 1 and len(c.takes) == 2 and c.media == "r3" and c.offset == 6000
    p.undo()
    p.undo()
    place(p, [(tr.id, "r4", 9000, 2000 + 3 * 10000 + 4000)], punch=(11000, 21000), loop=True)
    c = p.track(tr.id).clips[-1]                                     # a loop: every whole pass a take
    assert [k.offset for k in c.takes] == [2000, 12000, 22000] and c.take == 2


def test_recorder_null_input_lines_up(tmp_path):
    """The null input and the null output share the clock: a recording begun while the project plays from
    frame F lands at F (within a block)."""
    import time
    from sound.mt.rec import Recorder
    from sound.player import Player
    p = project(tmp_path)
    tr = p.add_track("A")
    from sound.mt.engine import Engine
    eng = Engine(p)
    pl = Player()
    try:
        p.add_clip(tr.id, "none", 0, RATE * 10)                      # something to play (an offline medium: silence)
        r = Recorder(p, str(tmp_path), str(tmp_path / "Audio"), RATE)
        assert r.open([p.track(tr.id)])
        assert pl.play(eng, 48000, RATE * 10)

        def clock(tm):
            pos, t0, _n = pl._clock
            return pos + (tm - t0) * RATE
        assert r.record(clock)
        time.sleep(0.8)
        out = r.stop()
        pl.stop()
        r.close()
        (tid, src, start), = out
        assert tid == tr.id and src.frames > RATE * 0.5
        assert abs(start - 48000) < 3 * 1024, start
        assert os.path.isfile(src.path) and src.path.endswith(".wav")
        assert abs(float(np.abs(src.read(0, src.frames)).max()) - 0.25) < 0.01    # the null input's tone
    finally:
        pl.stop()
        eng.close()
