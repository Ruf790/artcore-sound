"""Sound for films (2.0), File → Return to video: the edited sound replaces a track of the film (or is added) with the
picture, the other tracks and the subtitles copied, every codec, language / title / default / delay, chapters from
the markers; checked by ffprobe; MP4 without PCM; a failed write leaves nothing; the document remembers its film;
the window's dialog."""
import json
import os
import subprocess

import numpy as np
import pytest
from conftest import pump

from sound import ff, film, media


def _ff(*a):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *a], check=True)


@pytest.fixture(scope="module")
def films(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("films"))
    mkv = os.path.join(d, "Фильм.mkv")
    _ff("-f", "lavfi", "-i", "testsrc2=s=160x120:r=24000/1001:d=3", "-f", "lavfi", "-i", "sine=f=440:d=3:r=48000",
        "-f", "lavfi", "-i", "sine=f=880:d=3:r=48000", "-filter_complex",
        "[1]pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0[a]", "-map", "0:v", "-map", "[a]", "-map", "2:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a:0", "ac3", "-c:a:1", "aac", "-ac:a:1", "2",
        "-metadata:s:a:0", "language=eng", "-disposition:a:0", "default", os.path.join(d, "raw.mkv"))
    srt = os.path.join(d, "s.srt")
    with open(srt, "w", encoding="utf-8") as f:
        f.write("1\n00:00:00,500 --> 00:00:02,000\nПривет\n")
    _ff("-i", os.path.join(d, "raw.mkv"), "-i", srt, "-map", "0", "-map", "1", "-c", "copy", mkv)
    mp4 = os.path.join(d, "film.mp4")
    _ff("-i", os.path.join(d, "raw.mkv"), "-map", "0", "-c", "copy", mp4)
    return d, mkv, mp4


def _probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_chapters", path],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def test_document_remembers_its_film(films, session):
    _d, mkv, _mp4 = films
    d = media.open_file(mkv, session.folder(), 1)
    assert d.film["path"] == os.path.abspath(mkv) and d.film["track"] == 1 and d.film["index"] == 2
    assert abs(d.film["fps"] - 24000 / 1001) < 1e-3
    from sound import recovery
    assert recovery.to_dict(d, session.path)["film"]["index"] == 2


@pytest.mark.parametrize("codec", film.ORDER)
def test_replace_every_codec(films, session, codec, tmp_path):
    if codec == "dts" and not film.dts_available():
        pytest.skip("no DTS encoder")
    _d, mkv, _mp4 = films
    d = media.open_file(mkv, session.folder(), 0)
    d.silence(0, 4800)                                  # an edit to find in the result
    out = str(tmp_path / f"out_{codec}.mkv")
    opts = film.Options(mode="replace", replace_index=d.film["index"], codec=codec, lang="rus", title="Дубляж",
                        default=True, out_path=out)
    res = film.return_to_video(d, opts)
    assert res.streams == {"video": 1, "audio": 2, "subtitle": 1, "attachment": 0}
    js = _probe(out)
    st = js["streams"]
    assert [s["codec_type"] for s in st] == ["video", "audio", "audio", "subtitle"]
    assert st[0]["codec_name"] == "h264" and st[2]["codec_name"] == "aac" and st[3]["codec_name"] == "subrip"
    a = st[1]
    assert a["codec_name"] in film.CODECS[codec].probe_names and int(a["channels"]) == 6
    assert a["tags"]["language"] == "rus" and a["tags"]["title"] == "Дубляж" and a["disposition"]["default"] == 1
    assert st[2]["disposition"]["default"] == 0
    # the sound itself: the silenced start, then the tone
    e = media.open_file(out, session.folder(), 0)
    x = e.read(0, e.frames)
    assert np.abs(x[:3000]).max() < 0.02 and np.abs(x[9600:20000]).max() > 0.08
    assert abs(e.frames / e.rate - d.frames / d.rate) < 0.05


def test_add_track_delay_chapters_mp4(films, session, tmp_path):
    _d, _mkv, mp4 = films
    d = media.open_file(mp4, session.folder(), 1)
    d.add_marker(48000, "Глава 1")
    d.add_region(72000, 120000, "Сцена")
    assert "pcm" not in film.codecs_for(mp4)
    out = str(tmp_path / "add.mp4")
    opts = film.Options(mode="add", codec="eac3", option="256", lang="bul", title="BG", default=False,
                        delay_ms=500, chapters="markers", out_path=out)
    res = film.return_to_video(d, opts)
    assert res.track == 2 and res.streams["audio"] == 3
    js = _probe(out)
    a = [s for s in js["streams"] if s["codec_type"] == "audio"]
    assert a[2]["codec_name"] == "eac3" and a[2]["tags"]["language"] == "bul" and a[2]["disposition"]["default"] == 0
    assert a[0]["disposition"]["default"] == 1                       # the film's default stays
    ch = js["chapters"]
    # moved with the delay (MP4 keeps chapters as a text track whose first entry starts at 0)
    assert len(ch) == 2 and abs(float(ch[1]["start_time"]) - 2.0) < 0.01 and ch[1]["tags"]["title"] == "Сцена"
    # the delay: half a second of silence first
    e = media.open_file(out, session.folder(), 2)
    x = e.read(0, e.frames)
    assert np.abs(x[:int(0.45 * e.rate)]).max() < 0.02 and np.abs(x[int(0.6 * e.rate):int(0.8 * e.rate)]).max() > 0.08


def test_negative_delay_cuts_and_overwrite(films, session, tmp_path):
    _d, mkv, _mp4 = films
    import shutil
    own = str(tmp_path / "own.mkv")
    shutil.copy(mkv, own)
    d = media.open_file(own, session.folder(), 0)
    opts = film.Options(mode="replace", replace_index=d.film["index"], codec="flac", delay_ms=-1000,
                        chapters="none", out_path=own)
    film.return_to_video(d, opts)
    e = media.open_file(own, session.folder(), 0)
    assert abs(e.frames / e.rate - 2.0) < 0.05
    assert not [f for f in os.listdir(tmp_path) if "artcore-part" in f]


def test_failure_leaves_nothing(films, session, tmp_path, monkeypatch):
    _d, mkv, _mp4 = films
    d = media.open_file(mkv, session.folder(), 0)
    out = str(tmp_path / "bad.mkv")
    monkeypatch.setattr(film, "verify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("check: test")))
    with pytest.raises(RuntimeError):
        film.return_to_video(d, film.Options(replace_index=d.film["index"], out_path=out))
    assert not os.path.exists(out) and not [f for f in os.listdir(tmp_path) if "artcore-part" in f]


def test_plan_shapes():
    info = ff.FileInfo(path="x.mkv", streams=[(0, "video", "h264"), (1, "audio", "ac3"), (2, "data", "bin"),
                                              (3, "subtitle", "ass"), (4, "attachment", "ttf")])
    args, pos, exp = film.plan(info, 8, 96000, "7.1", film.Options(codec="ac3", replace_index=1))
    assert pos == 0 and exp == {"video": 1, "audio": 1, "subtitle": 1, "attachment": 1}
    assert "0:2" not in args and "aresample=ochl=5.1" in args and "48000" in args
    args, pos, exp = film.plan(info, 2, 48000, "stereo", film.Options(mode="add", codec="opus"))
    assert pos == 1 and exp["audio"] == 2 and args[args.index("-map", args.index("0:1")) + 1] == "1:a:0"
    assert film.out_shape("dts", 6, 48000)[2] == "5.1(side)"
    assert film.default_out("/x/Фильм.mkv").endswith("Фильм (ArtCore).mkv")


def test_return_dialog(qapp, session, data_dir, films):
    _d, mkv, _mp4 = films
    from sound import config
    from sound.filmui import ReturnDialog
    d = media.open_file(mkv, session.folder(), 0)
    cfg = config.load()
    dlg = ReturnDialog(d, cfg)
    dlg.show()
    pump(qapp, 0.1)
    assert dlg.m_replace.isChecked() and dlg.track.count() == 2 and dlg.track.currentData() == d.film["index"]
    assert dlg.codec.currentData() == "ac3" and dlg.lang.currentText().startswith("eng") and dlg.default.isChecked()
    dlg.codec.setCurrentIndex(dlg.codec.findData("aac"))
    dlg.m_add.setChecked(True)
    dlg.delay.setValue(1001)
    assert "+24.0" in dlg.delay_frames.text()
    o = dlg.options()
    assert o.mode == "add" and o.codec == "aac" and o.out_path.endswith("Фильм (ArtCore).mkv") and o.delay_ms == 1001
    dlg.overwrite.setChecked(True)
    assert dlg.options().out_path == mkv
    assert dlg.stereo.isEnabled() and not dlg.options().stereo
    dlg.stereo.setChecked(True)
    dlg.stereo_how.setCurrentIndex(dlg.stereo_how.findData("pl2"))
    dlg.title.setText("Дубляж")
    o = dlg.options()
    assert o.stereo == "pl2" and o.stereo_title.startswith("Дубляж (") and "192" in dlg.note.text()
    dlg.remember()
    assert cfg["return_video"]["codec"] == "aac" and cfg["return_video"]["stereo_how"] == "pl2"
    dlg.reject()
    st = media.open_file(mkv, session.folder(), 1)                     # a stereo document: nothing to add
    dlg = ReturnDialog(st, cfg)
    assert not dlg.stereo.isEnabled() and not dlg.options().stereo
    dlg.reject()


# ── every channel in its place (2.0, block 2) ─────────────────
TONES = {"5.1(side)": (310, 530, 730, 60, 1110, 1330), "7.1": (310, 530, 730, 60, 950, 1170, 1390, 1610)}
NAMES = {"5.1(side)": ("FL", "FR", "FC", "LFE", "SL", "SR"),
         "7.1": ("FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR")}


def _tones_doc(session, d, layout):
    path = os.path.join(d, f"tones_{layout}.wav")
    if not os.path.exists(path):
        args = []
        for f in TONES[layout]:
            args += ["-f", "lavfi", "-i", f"sine=f={f}:d=3:r=48000"]
        n = len(TONES[layout])
        mp = "|".join(f"{i}.0-{c}" for i, c in enumerate(NAMES[layout]))
        _ff(*args, "-filter_complex", "".join(f"[{i}]" for i in range(n)) +
            f"join=inputs={n}:channel_layout={layout}:map={mp}[a]", "-map", "[a]", "-c:a", "pcm_f32le", path)
    return media.open_file(path, session.folder(), 0)


def _dominant(x, rate):
    seg = x[rate // 2: rate // 2 + rate] * np.hanning(rate)[:, None]
    return [int(np.argmax(np.abs(np.fft.rfft(seg[:, c])))) for c in range(x.shape[1])]


def _check_places(e, freqs):
    got = _dominant(e.read(0, e.frames), e.rate)
    assert got == list(freqs), (e.labels, got)


@pytest.mark.parametrize("ext", [".mkv", ".mp4"])
@pytest.mark.parametrize("codec", film.ORDER)
def test_every_channel_in_its_place(films, session, codec, ext, tmp_path):
    """A different tone in each channel → Return to video → each tone in its own channel, the layout checked."""
    if codec == "dts" and not film.dts_available():
        pytest.skip("no DTS encoder")
    dd, mkv, mp4 = films
    if codec not in film.codecs_for("film" + ext):
        pytest.skip("not in this container")
    d = _tones_doc(session, dd, "5.1(side)")
    assert _dominant(d.read(0, d.frames), d.rate) == list(TONES["5.1(side)"])
    src = mkv if ext == ".mkv" else mp4
    d.film = {"path": src, "track": 0, "index": 1, "fps": 24}
    out = str(tmp_path / f"places_{codec}{ext}")
    res = film.return_to_video(d, film.Options(replace_index=1, codec=codec, out_path=out))
    assert res.channels == 6
    a = [s for s in _probe(out)["streams"] if s["codec_type"] == "audio"][0]
    assert film.layout_ok(a.get("channel_layout", ""), 6, "5.1(side)")
    e = media.open_file(out, session.folder(), 0)
    assert e.labels == ("L", "R", "C", "LFE", "Ls", "Rs")
    _check_places(e, TONES["5.1(side)"])


@pytest.mark.parametrize("codec", [k for k in film.ORDER if film.CODECS[k].max_channels >= 8])
def test_seven_one_in_its_place(films, session, codec, tmp_path):
    dd, mkv, _mp4 = films
    d = _tones_doc(session, dd, "7.1")
    d.film = {"path": mkv, "track": 0, "index": 1, "fps": 24}
    out = str(tmp_path / f"places71_{codec}.mkv")
    film.return_to_video(d, film.Options(replace_index=1, codec=codec, out_path=out))
    e = media.open_file(out, session.folder(), 0)
    assert e.channels == 8
    _check_places(e, TONES["7.1"])


def test_layout_check():
    assert film.layout_ok("5.1", 6, "5.1(side)") and film.layout_ok("5.1(side)", 6, "5.1")
    assert film.layout_ok("", 6, "5.1(side)") and film.layout_ok("unknown", 2, "stereo")
    assert not film.layout_ok("6.0", 6, "5.1") and not film.layout_ok("6 channels", 6, "5.1")
    assert not film.layout_ok("hexagonal", 6, "5.1") and not film.layout_ok("7.1(wide)", 8, "7.1")


def test_verify_refuses_a_wrong_layout(films, session, tmp_path):
    dd, mkv, _mp4 = films
    bad = str(tmp_path / "hex.mkv")
    _ff("-i", mkv, "-map", "0:v", "-map", "0:a:0", "-c:v", "copy", "-af", "aformat=channel_layouts=6.0",
        "-c:a", "flac", bad)
    with pytest.raises(RuntimeError, match="layout"):
        film.verify(bad, {"video": 1, "audio": 1, "subtitle": 0, "attachment": 0}, 0, "flac", 6, 3.0, "5.1")


@pytest.mark.parametrize("kind", ["itu", "pl2"])
def test_stereo_track_added(films, session, kind, tmp_path):
    """«Add a stereo track»: after the 5.1 one, its own codec settings, not default; the front tones in their sides,
    the centre in both, the 5.1 track untouched."""
    dd, mkv, _mp4 = films
    d = _tones_doc(session, dd, "5.1(side)")
    d.film = {"path": mkv, "track": 0, "index": 1, "fps": 24}
    out = str(tmp_path / f"stereo_{kind}.mkv")
    res = film.return_to_video(d, film.Options(replace_index=1, codec="ac3", lang="rus", title="Дубляж", out_path=out,
                                               stereo=kind, stereo_title="Дубляж (стерео)"))
    assert res.track == 0 and res.stereo_track == 1
    assert res.streams == {"video": 1, "audio": 3, "subtitle": 1, "attachment": 0}
    a = [s for s in _probe(out)["streams"] if s["codec_type"] == "audio"]
    assert [int(s["channels"]) for s in a] == [6, 2, 2]
    assert a[1]["tags"]["title"] == "Дубляж (стерео)" and a[1]["tags"]["language"] == "rus"
    assert a[0]["disposition"]["default"] == 1 and a[1]["disposition"]["default"] == 0
    assert int(a[1]["bit_rate"]) == 256000
    _check_places(media.open_file(out, session.folder(), 0), TONES["5.1(side)"])
    st = media.open_file(out, session.folder(), 1)
    x = st.read(0, st.frames)
    seg = x[24000:72000] * np.hanning(48000)[:, None]
    spec = np.abs(np.fft.rfft(seg, axis=0))
    lvl = lambda f, c: spec[f - 2:f + 3, c].max()                         # noqa: E731
    fl, fr, fc, _lfe, sl, sr = TONES["5.1(side)"]
    assert lvl(fl, 0) > 30 * lvl(fl, 1) and lvl(fr, 1) > 30 * lvl(fr, 0)
    assert 0.8 < lvl(fc, 0) / lvl(fc, 1) < 1.25
    if kind == "itu":
        assert lvl(sl, 0) > 30 * lvl(sl, 1) and lvl(sr, 1) > 30 * lvl(sr, 0)
    else:                                                             # Lt/Rt: both surrounds in both sides
        assert lvl(sl, 1) > 0.3 * lvl(sl, 0) and lvl(sr, 0) > 0.3 * lvl(sr, 1)
    assert abs(st.frames - d.frames) < 0.02 * d.rate
    # the downmix lines up with the picture: the front left tone starts where it does in the 5.1 track
    m = media.open_file(out, session.folder(), 0).read(0, 4800)[:, 0]
    s0 = x[:4800, 0]
    lag = int(np.argmax(np.correlate(s0, m, "full"))) - (len(m) - 1)
    assert abs(lag) <= 2


def test_stereo_plan_only_for_multichannel():
    info = ff.FileInfo(path="x.mkv", streams=[(0, "video", "h264"), (1, "audio", "ac3")])
    args, pos, exp = film.plan(info, 2, 48000, "stereo", film.Options(replace_index=1, stereo="itu"))
    assert exp["audio"] == 1 and "-filter_complex" not in args
    args, pos, exp = film.plan(info, 8, 48000, "7.1", film.Options(mode="add", codec="ac3", stereo="loro"))
    assert pos == 1 and exp["audio"] == 3
    fc = args[args.index("-filter_complex") + 1]
    assert "aresample=ochl=5.1" in fc and "c0=c8|c1=c9[st]" in fc and args[args.index("-ch_layout") + 1] == "10c"


@pytest.mark.parametrize("codec", ["ac3", "eac3", "dts"])
def test_seven_one_folded_to_five_one(films, session, codec, tmp_path):
    """AC-3, E-AC-3 and DTS take 5.1 at most: 7.1 is folded down, the front channels and the LFE stay in place."""
    if codec == "dts" and not film.dts_available():
        pytest.skip("no DTS encoder")
    dd, mkv, _mp4 = films
    d = _tones_doc(session, dd, "7.1")
    d.film = {"path": mkv, "track": 0, "index": 1, "fps": 24}
    out = str(tmp_path / f"fold_{codec}.mkv")
    res = film.return_to_video(d, film.Options(replace_index=1, codec=codec, out_path=out))
    assert res.channels == 6
    e = media.open_file(out, session.folder(), 0)
    assert _dominant(e.read(0, e.frames), e.rate)[:4] == list(TONES["7.1"][:4])
