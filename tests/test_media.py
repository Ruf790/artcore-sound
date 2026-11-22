"""Opening and saving through ffmpeg with real files; a long 5.1 file in little memory; playback timing."""
import os
import time
import tracemalloc

import numpy as np
import pytest

from conftest import ff
from sound import ff as F
from sound import media, player, proc


@pytest.mark.parametrize("name,rate,ch,codec", [
    ("stereo16.wav", 44100, 2, "pcm_s16le"), ("mono24.wav", 48000, 1, "pcm_s24le"),
    ("hires.flac", 96000, 2, "flac"), ("song.mp3", 44100, 2, "mp3"), ("song.m4a", 44100, 2, "aac"),
    ("film.ac3", 48000, 6, "ac3"), ("film.eac3", 48000, 6, "eac3"), ("film.dts", 48000, 6, "dts"),
    ("voice.opus", 48000, 2, "opus"), ("song.ogg", 44100, 2, "vorbis"), ("song.wma", 44100, 2, "wmav2")])
def test_open_formats(media_dir, session, name, rate, ch, codec):
    path = os.path.join(media_dir, name)
    info = F.probe(path)
    assert info.tracks[0].codec == codec
    d = media.open_file(path, session.folder())
    assert (d.rate, d.channels) == (rate, ch)
    dur = 2.0 if name in ("mono24.wav", "hires.flac") else 3.0
    assert abs(d.frames / rate - dur) < 0.08, d.frames
    assert 0.05 < d.peak().max() < 1.01
    if ch == 6:
        assert d.labels == ("L", "R", "C", "LFE", "Ls", "Rs")
        assert d.peak()[1] < d.peak()[0]                 # the channels are not shuffled: R was made quieter
    if name == "stereo16.wav":
        x = d.read(0, d.frames)
        assert abs(np.abs(x[:, 0]).max() - 0.5) < 1e-3 and abs(np.abs(x[:, 1]).max() - 0.25) < 1e-3
        assert d.save_format == "wav" and d.save_opts == {"option": "16"}
    if name == "hires.flac":
        assert d.track.bits == 24 and d.save_opts == {"option": "24"}
    if name == "film.dts":
        assert d.save_format == ""                       # Save asks for a format: there is no DTS encoder


def test_open_a_film_track(media_dir, session):
    path = os.path.join(media_dir, "Фильм (2024).mkv")
    info = F.probe(path)
    assert info.has_video and [t.channels for t in info.tracks] == [6, 2]
    assert info.tracks[0].lang == "rus" and info.tracks[0].title == "Дубляж" and info.tracks[0].default
    d = media.open_file(path, session.folder(), track_pos=1)
    assert d.channels == 2 and abs(d.frames - 3 * 48000) < 2048
    assert d.save_format == ""                           # sound from a video: Save asks
    x = d.read(24000, 4800)[:, 0]                        # the 880 Hz track, not the 440 one
    zc = np.count_nonzero(np.diff(np.signbit(x)))
    assert abs(zc / 2 / 0.1 - 880) < 30


def test_broken_and_missing(media_dir, session):
    with pytest.raises(Exception):
        media.open_file(os.path.join(media_dir, "broken.wav"), session.folder())
    with pytest.raises(Exception):
        F.probe(os.path.join(media_dir, "nope.wav"))


def test_save_every_format(media_dir, session, tmp_path):
    d = media.open_file(os.path.join(media_dir, "film.ac3"), session.folder())
    want = {"wav": ("pcm_s24le", 6), "flac": ("flac", 6), "mp3": ("mp3", 2), "aac": ("aac", 6),
            "opus": ("opus", 6), "ac3": ("ac3", 6)}
    for fmt, f in media.FORMATS.items():
        for opt in (f.options[0], f.options[-1]):
            out = str(tmp_path / f"o_{fmt}_{opt.replace(':', '_')}{f.ext}")
            media.save(d, out, fmt, opt)
            info = F.probe(out)
            tr = info.tracks[0]
            if fmt == "wav":
                assert tr.codec == {"16": "pcm_s16le", "24": "pcm_s24le", "32f": "pcm_f32le"}[opt]
            elif fmt == "flac":
                assert tr.bits == int(opt.split(":")[0])
            else:
                assert tr.codec == want[fmt][0]
            assert tr.channels == want[fmt][1]
            assert abs(info.duration - d.frames / d.rate) < 0.1
            assert not [x for x in os.listdir(tmp_path) if "artcore-part" in x]


def test_save_round_trip_and_selection(media_dir, session, tmp_path):
    d = media.open_file(os.path.join(media_dir, "stereo16.wav"), session.folder())
    out = str(tmp_path / "f32.wav")
    media.save(d, out, "wav", "32f")
    e = media.open_file(out, session.folder())
    np.testing.assert_array_equal(e.read(0, e.frames), d.read(0, d.frames))     # float WAV: bit for bit
    out16 = str(tmp_path / "s16.flac")
    media.save(d, out16, "flac", "16")
    g = media.open_file(out16, session.folder())
    assert np.abs(g.read(0, g.frames) - d.read(0, d.frames)).max() < 2 / 32768    # dither: an LSB or so
    part = str(tmp_path / "part.wav")
    media.save(d, part, "wav", "24", a=44100, b=66150)
    assert F.probe(part).tracks[0].duration == pytest.approx(0.5, abs=0.001)
    media.save(d, out, "wav", "24", tags={"title": "Проба", "artist": "ArtCore"})
    assert F.probe(out).tags.get("title") == "Проба"


def test_mp3_and_ac3_shape():
    assert media.out_shape("mp3", 6, 96000) == (2, 48000)
    assert media.out_shape("ac3", 8, 96000) == (6, 48000)
    assert media.out_shape("ac3", 2, 44100) == (2, 44100)
    assert media.out_shape("opus", 2, 44100) == (2, 48000)
    assert media.out_shape("wav", 8, 192000) == (8, 192000)


def test_long_51_file_in_little_memory(tmp_path, session):
    """Six minutes of 5.1 at 48 kHz (400 MB as float): opening, drawing the whole file and every zoom, a fade and
    saving it never hold more than a few megabytes in memory — the sound stays on disk."""
    path = str(tmp_path / "long.ac3")
    ff("-f", "lavfi", "-i", "sine=f=220:d=360:r=48000", "-filter_complex",
       "[0]pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0", "-c:a", "ac3", "-b:a", "192k", path)
    tracemalloc.start()
    t0 = time.monotonic()
    d = media.open_file(path, session.folder())
    t_open = time.monotonic() - t0
    assert d.frames >= 360 * 48000 - 2048 and d.channels == 6
    for spp in (d.frames / 1500, 50000, 5000, 900, 40, 1):
        mm = d.minmax(d.frames / 3, spp, 1500)
        assert not np.isnan(mm[:10]).any()
    src = proc.apply_op(d, "fade_out", d.frames - 48000 * 60, d.frames)
    d.replace(d.frames - 48000 * 60, d.frames, src, "fade out")
    media.save(d, str(tmp_path / "long.flac"), "flac", "16")
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 40 * 1024 * 1024, f"peak {peak / 1e6:.1f} MB"
    assert d.disk_bytes() > 400e6                        # the sound is on disk instead
    print(f"open {t_open:.1f} s, peak memory {peak / 1e6:.1f} MB, on disk {d.disk_bytes() / 1e6:.0f} MB")


def test_convert_rate_and_channels(media_dir, session):
    d = media.open_file(os.path.join(media_dir, "film.ac3"), session.folder())
    e = media.open_file(os.path.join(media_dir, "stereo16.wav"), session.folder())
    clip = d.copy(0, 48000)
    assert not e.fits(clip)
    c2 = proc.convert_clip(clip, e)
    assert e.fits(c2) and c2.frames == 44100
    e.paste(c2, 0)
    assert e.frames == 3 * 44100 + 44100
    src = proc.convert_document(d, 44100, 2, "stereo")
    assert src.channels == 2 and abs(src.frames - d.frames * 44100 / 48000) < 64


def test_player_null_output(media_dir, session):
    d = media.open_file(os.path.join(media_dir, "stereo16.wav"), session.folder())
    p = player.Player()
    assert p.play(d, 22050, 44100) and p.backend == "null"
    time.sleep(0.25)
    pos = p.position()
    assert 22050 + 0.1 * 44100 < pos < 22050 + 0.45 * 44100
    lv = p.levels()
    assert lv is not None and abs(lv[0] - 0.5) < 0.02 and abs(lv[1] - 0.25) < 0.02
    t0 = time.monotonic()
    while not p.done and time.monotonic() - t0 < 3:
        time.sleep(0.02)
    assert p.done and p.position() == 44100
    p.stop()
    assert p.play(d, 0, 4410, loop=True)
    seen = set()
    for _ in range(30):
        time.sleep(0.02)
        seen.add(p.position() // 1000)
    assert not p.done and len(seen) >= 4 and max(seen) <= 4
    p.pause()
    a = p.position()
    time.sleep(0.15)
    assert p.position() == a and p.state == "pause"
    assert p.resume() and p.state == "play"
    p.stop()
    assert p.state == "stop"


def test_mix_matrix():
    m = player.mix_matrix(("L", "R", "C", "LFE", "Ls", "Rs"), 2)
    assert m.shape == (6, 2) and m[3].sum() == 0 and m[0, 1] == 0 and m[1, 0] == 0
    assert m.sum(axis=0).max() <= 1.0001                 # no channel can clip after the downmix
    assert (player.mix_matrix(("L", "R"), 6)[:, :2] == np.eye(2)).all()
    assert np.allclose(player.mix_matrix(("L", "R"), 1), 0.5)
