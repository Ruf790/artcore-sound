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
    dlg.remember()
    assert cfg["return_video"]["codec"] == "aac"
    dlg.reject()
