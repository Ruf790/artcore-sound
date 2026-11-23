"""Markers and regions (0.4): how every kind of edit moves them (against positions worked out by hand), undo / redo,
the clipboard, WAV cue / labl written and read back (also a file laid out as other programs write them, and RF64),
the tags / chapters of the other formats, a film's chapters, CSV / .cue / Audacity lists, regions saved as files."""
import struct
import subprocess

import numpy as np
import pytest

from sound import doc as D
from sound import ff, marks, media, store

R = 1000                      # frames per «second» in the edit tests (positions read easily)


def _doc(session, frames=10 * R, ch=2, rate=R):
    d = D.new_document(session.folder(), rate, ch)
    x = (np.arange(frames * ch, dtype=np.float32).reshape(frames, ch) % 97) / 100.0
    d.set_initial(store.from_array(d.folder, x))
    return d


def _setup(d):
    """Markers at 1, 3, 5, 7, 9 s; regions [2, 4), [4.5, 6.5), [6, 8)."""
    for p in (1, 3, 5, 7, 9):
        d.add_marker(p * R, f"m{p}")
    d.add_region(2 * R, 4 * R, "r1")
    d.add_region(int(4.5 * R), int(6.5 * R), "r2")
    d.add_region(6 * R, 8 * R, "r3")


def _state(d):
    return sorted((m.name, m.pos, m.end) for m in d.markers)


def test_remap_basics():
    ms = (D.new_marker("a", 100), D.new_marker("b", 200), D.new_marker("c", 300), D.new_marker("r", 150, 350))
    got = {m.name: (m.pos, m.end) for m in D.remap_markers(ms, 200, 300, 0)}
    # the marker at the cut start stays, the one at its end moves to the seam; the region loses 100 frames
    assert got == {"a": (100, None), "b": (200, None), "c": (200, None), "r": (150, 250)}
    got = {m.name: (m.pos, m.end) for m in D.remap_markers(ms, 120, 250, 0)}
    assert got == {"a": (100, None), "c": (170, None), "r": (120, 220)}         # b was inside: gone
    got = {m.name: (m.pos, m.end) for m in D.remap_markers(ms, 100, 300, 100, "scale")}
    assert got == {"a": (100, None), "b": (150, None), "c": (200, None), "r": (125, 250)}
    got = {m.name: (m.pos, m.end) for m in D.remap_markers(ms, 150, 150, 50)}  # an insertion at the region start
    assert got == {"a": (100, None), "b": (250, None), "c": (350, None), "r": (200, 400)}
    got = {m.name: (m.pos, m.end) for m in D.remap_markers(ms, 0, 400, 0)}
    assert got == {}


def test_every_edit_moves_markers(session):
    d = _doc(session)
    _setup(d)
    v = d.version
    assert len(d.markers) == 8 and d.version == v                 # marker steps leave the sound's version
    base = _state(d)

    d.delete(int(2.5 * R), int(5.5 * R))                          # 3 s of sound gone
    assert _state(d) == sorted([("m1", 1000, None), ("m7", 4000, None), ("m9", 6000, None),
                                ("r1", 2000, 2500), ("r2", 2500, 3500), ("r3", 3000, 5000)])
    d.undo()
    assert _state(d) == base

    clip = d.cut(3 * R, 5 * R)                                     # m3 (at the start) goes with the clip
    assert ("m3", 3000, None) not in _state(d) and ("m5", 3000, None) in _state(d)
    assert [(n, p, e) for n, p, e in clip.markers] == [("m3", 0, None)]
    d.paste(clip, 8 * R)                                           # 8 s now = the old 10 s… ends at 8 + 2
    names = [m.name for m in d.markers]
    assert names.count("m3") == 1 and any(m.name == "m3" and m.pos == 8000 for m in d.markers)
    d.undo()
    d.undo()
    assert _state(d) == base

    d.insert_silence(5 * R, 2 * R)                                 # the marker at 5 s stays before the silence
    st = dict(((n, (p, e)) for n, p, e in _state(d)))
    assert st["m5"] == (5000, None) and st["m7"] == (9000, None) and st["r2"] == (4500, 8500)
    assert st["r3"] == (8000, 10000)
    d.undo()

    d.trim(2 * R, 8 * R)                                           # keep [2, 8)
    assert _state(d) == sorted([("m3", 1000, None), ("m5", 3000, None), ("m7", 5000, None),
                                ("r1", 0, 2000), ("r2", 2500, 4500), ("r3", 4000, 6000)])
    d.undo()

    d.keep(0, 10 * R, int(0.5 * R), int(9.5 * R))                  # trim silence: 0.5 s off each end
    assert ("m1", 500, None) in _state(d) and ("m9", 8500, None) in _state(d)
    d.undo()

    d.silence(2 * R, 4 * R)                                        # the same length: nothing moves
    assert _state(d) == base
    d.undo()

    src = store.from_array(d.folder, np.zeros((2 * R, 2), np.float32))
    d.replace(4 * R, 8 * R, src, "tempo ×2")                       # an effect that halves [4, 8): scaled
    st = dict(((n, (p, e)) for n, p, e in _state(d)))
    assert st["m5"] == (4500, None) and st["m7"] == (5500, None) and st["m9"] == (7000, None)
    assert st["r2"] == (4250, 5250) and st["r3"] == (5000, 6000) and st["r1"] == (2000, 4000)
    d.undo()

    src = store.from_array(d.folder, np.zeros((20 * R, 2), np.float32))
    d.replace(0, d.frames, src, "rate ×2", rate=2 * R)             # a new rate: everything scaled
    assert _state(d) == sorted((n, p * 2, None if e is None else e * 2) for n, p, e in base)
    d.undo()
    assert _state(d) == base and d.rate == R

    src = store.from_array(d.folder, np.zeros((R, 2), np.float32))
    d.replace(4 * R, 8 * R, src, "record", remap="cut", add_markers=[("rec", 500, None)])
    st = dict(((n, (p, e)) for n, p, e in _state(d)))
    assert "m5" not in st and "m7" not in st and st["rec"] == (4500, None) and st["m9"] == (6000, None)
    assert "r2" not in st and "r3" not in st and st["r1"] == (2000, 4000)     # their sound is all gone
    d.undo()
    assert _state(d) == base


def test_marker_steps_and_undo(session):
    d = _doc(session)
    m = d.add_marker(1234, "x")
    v = d.version
    assert d.dirty and d.undo_label() == "add marker"
    d.update_marker(m.id, pos=2000)
    d.update_marker(m.id, name="y", label="rename marker")
    assert (d.marker(m.id).name, d.marker(m.id).pos) == ("y", 2000)
    r = d.add_region(3000, 5000, "reg")
    d.update_marker(r.id, pos=4000)                                 # moved whole: the length stays
    assert (d.marker(r.id).pos, d.marker(r.id).end) == (4000, 6000)
    d.update_marker(r.id, end=4500)
    assert d.marker(r.id).end == 4500
    d.remove_markers([m.id])
    assert d.marker(m.id) is None
    assert d.version == v                                           # never the sound
    for _ in range(5):
        d.undo()
    assert d.marker(m.id).name == "x" and d.marker(r.id) is None
    d.redo()
    d.redo()
    assert d.marker(m.id).name == "y"
    assert d.marker_points() == [2000, 3000, 5000]
    d.goto_state(0)
    assert d.markers == ()


def _sine_doc(session, secs=3, rate=48000, ch=2):
    d = D.new_document(session.folder(), rate, ch)
    t = np.arange(secs * rate) / rate
    x = np.stack([0.3 * np.sin(2 * np.pi * 440 * t)] * ch, 1).astype(np.float32)
    d.set_initial(store.from_array(d.folder, x))
    return d


@pytest.mark.parametrize("fmt", ["wav", "flac", "mp3", "aac", "opus"])
def test_formats_keep_markers(session, tmp_path, fmt):
    d = _sine_doc(session)
    d.add_marker(48000, "Метка «A»")
    d.add_region(72000, 120000, "Reg; B=1\ttab")
    d.add_marker(130000, "C")
    path = str(tmp_path / ("out" + media.FORMATS[fmt].ext))
    media.save(d, path, fmt, media.default_option(fmt, 2))
    d2 = media.open_file(path, session.folder())
    assert _state(d2) == _state(d)
    info = ff.probe(path)
    if fmt in ("flac", "mp3", "aac", "opus"):                      # what other programs see: chapters
        titles = [c[2] for c in info.chapters]
        assert "Метка «A»" in titles and "C" in titles
    if fmt == "wav":                                               # the audio itself intact
        assert info.tracks[0].codec == "pcm_s24le" and abs(info.duration - 3.0) < 0.01


def test_selection_save_and_rate(session, tmp_path):
    d = _sine_doc(session)
    d.add_marker(48000, "a")
    d.add_region(24000, 96000, "r")
    path = str(tmp_path / "part.wav")
    media.save(d, path, "wav", "16", a=36000, b=144000)
    assert marks.read_wav(path) == [("r", 0, 60000), ("a", 12000, None)]   # the region cut to the part saved
    assert marks.items_for_save(d, 0, d.frames, 24000) == [("r", 12000, 48000), ("a", 24000, None)]


def _riff(chunks):
    body = b"WAVE" + b"".join(c + struct.pack("<I", len(d)) + d + (b"\0" if len(d) & 1 else b"") for c, d in chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_wav_from_other_software(session, tmp_path):
    """The layout other editors use: cue before the data, positions in dwPosition only, «note» texts, a LIST INFO,
    odd sizes padded, Windows-1251 names — read right, and ffmpeg still plays the sound."""
    rate, n = 44100, 44100
    pcm = (np.sin(np.arange(n) * 2 * np.pi * 440 / rate) * 12000).astype("<i2").tobytes()
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    cue = struct.pack("<I", 3) + b"".join(struct.pack("<II4sIII", i, p, b"data", 0, 0, 0)
                                          for i, p in ((1, 1000), (2, 22050), (7, 30000)))
    adtl = b"adtl"
    for sid, body in ((b"labl", struct.pack("<I", 1) + "Начало".encode("cp1251") + b"\0"),
                      (b"labl", struct.pack("<I", 2) + b"Chorus\0"),
                      (b"ltxt", struct.pack("<II4sHHHH", 2, 11025, b"rgn ", 0, 0, 0, 0)),
                      (b"note", struct.pack("<I", 7) + b"odd\0")):
        adtl += sid + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b"")
    info = b"INFO" + b"ISFT" + struct.pack("<I", 5) + b"Wav0\0" + b"\0"
    path = str(tmp_path / "other.wav")
    with open(path, "wb") as f:
        f.write(_riff([(b"fmt ", fmt), (b"cue ", cue), (b"LIST", info), (b"data", pcm), (b"LIST", adtl)]))
    assert marks.read_wav(path) == [("Начало", 1000, None), ("Chorus", 22050, 33075), ("odd", 30000, None)]
    d = media.open_file(path, session.folder())
    assert d.frames == n and _state(d) == sorted([("Начало", 1000, None), ("Chorus", 22050, 33075),
                                                    ("odd", 30000, None)])
    assert abs(float(d.peak().max()) - 12000 / 32768) < 1e-3


def test_wav_rf64(tmp_path):
    path = str(tmp_path / "big.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=1:r=48000", "-c:a", "pcm_s16le",
                    "-rf64", "always", path], check=True)
    with open(path, "rb") as f:
        assert f.read(4) == b"RF64"
    marks.write_wav(path, [("a", 100, None), ("b", 200, 1200)])
    assert marks.read_wav(path) == [("a", 100, None), ("b", 200, 1200)]
    info = ff.probe(path)
    assert abs(info.duration - 1.0) < 0.01
    with open(path, "rb") as f:                                    # ds64's riff size = the file − 8
        data = f.read()
    i = data.index(b"ds64")
    assert struct.unpack("<Q", data[i + 8:i + 16])[0] == len(data) - 8


def test_film_chapters_open_as_regions(session, tmp_path):
    meta = tmp_path / "ch.txt"
    meta.write_text(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1500\ntitle=Пролог\n"
                    "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1500\nEND=3000\ntitle=Part 2\n", encoding="utf-8")
    path = str(tmp_path / "film.mkv")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=160x120:r=10:d=3", "-f", "lavfi",
                    "-i", "sine=d=3:r=48000", "-i", str(meta), "-map", "0", "-map", "1", "-map_chapters", "2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "ac3", path], check=True)
    d = media.open_file(path, session.folder())
    assert [(m.name, m.pos, m.end) for m in d.markers] == [("Пролог", 0, 72000), ("Part 2", 72000, 144000)]


def test_lists_roundtrip(session, tmp_path):
    d = _sine_doc(session)
    d.add_marker(48000, "a, \"quoted\"")
    d.add_region(72000, 120000, "Регион")
    p = str(tmp_path / "m.csv")
    marks.export_file(p, d.markers, d.rate)
    got = marks.import_file(p, d.rate, d.frames)
    assert got == [("a, \"quoted\"", 48000, None), ("Регион", 72000, 120000)]
    assert marks.import_file(p, 96000, 2 * d.frames)[0][1] == 48000      # samples win over the times
    p = str(tmp_path / "m.cue")
    marks.export_file(p, d.markers, d.rate, "song.wav")
    txt = open(p, encoding="utf-8").read()
    assert 'FILE "song.wav" WAVE' in txt and "INDEX 01 00:01:37" in txt         # 1.5 s = 1 s 37.5 frames → 38?
    got = marks.import_file(p, d.rate, d.frames)
    assert got[0][0] == "Регион" and abs(got[0][1] - 72000) <= 640 and got[0][2] == d.frames
    p = str(tmp_path / "labels.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("1.000000\t1.000000\tpoint\n1.5\t2.25\tspan\n")
    assert marks.import_file(p, 48000, d.frames) == [("point", 48000, None), ("span", 72000, 108000)]
    p = str(tmp_path / "simple.csv")
    with open(p, "w", encoding="utf-8") as f:
        f.write("intro;0:00:01.000;0:00:02.000\nend;0:00:02.500;\n")
    assert marks.import_file(p, 1000, 3000) == [("intro", 1000, 2000), ("end", 2500, None)]
    d.set_markers(marks.import_file(p, d.rate, d.frames), label="markers")
    assert len(d.markers) == 4 and d.undo_label() == "markers"


def test_cue_time():
    assert marks.cue_time(1.5) in ("00:01:37", "00:01:38")
    assert marks.cue_time(61.0) == "01:01:00"
    assert marks.safe_name('a/b:c*?"<>|. ', "x") == "a_b_c______"
    assert marks.safe_name("...", "x") == "x"


def test_tag_text_roundtrip():
    items = [("a\tb\\c\nd", 10, None), ("r", 20, 30)]
    assert marks.parse_tag(marks.tag_text(items, 48000), 48000) == items
    assert marks.parse_tag(marks.tag_text(items, 48000), 96000) == [("a\tb\\c\nd", 20, None), ("r", 40, 60)]
    assert marks.parse_tag("garbage", 48000) is None
