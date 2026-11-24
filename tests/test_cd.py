"""Audio CD (2.0, block 4): the table of contents (times, an Enhanced CD's data track, the MusicBrainz disc ID),
CD-TEXT (packs and their CRC, back from packs read in any order or damaged, Cyrillic in Latin letters), the raw
Disc-At-Once image (the lead-in's table of contents and CD-TEXT, the Q subcode's CRC everywhere, the pregaps as
index 0 with the pause flag, the sound where it belongs), .cue + .bin; ripping from a drive (a fake one: the sound
exact, the drive's offset made up for, pieces that read wrong read again, those that never agree reported), into
files with tags or the editor; burning a document's regions; a disc ripped and burnt back is the same disc."""
import json
import os

import numpy as np
import pytest
from conftest import ff, pump

from sound import store
from sound.cd import burn as cdb
from sound.cd import cdtext as C
from sound.cd import drive as cdd
from sound.cd import image as I
from sound.cd import musicbrainz as MB
from sound.cd import rip as R
from sound.cd import toc as T

SPF = 588


def _pcm(n_sectors, seed=0):
    return np.random.default_rng(seed).integers(-20000, 20000, n_sectors * SPF * 2).astype("<i2").tobytes()


# ── the table of contents ─────────────────────────────────────
def test_toc_times_and_lengths():
    assert T.msf(0) == (0, 2, 0) and T.msf(0, absolute=False) == (0, 0, 0)
    assert T.msf(I.LEADIN_START) == (95, 0, 0)                     # the image starts at MSF 95:00:00
    assert T.msf_text(4500 + 75 * 3 + 7) == "01:03:07"
    toc = T.Toc([T.Track(1, 0), T.Track(2, 10000), T.Track(3, 30000, audio=False)], 50000).finish()
    assert [x.length for x in toc.tracks] == [10000, 30000 - T.SESSION_GAP - 10000, 20000]
    assert [x.number for x in toc.audio] == [1, 2] and toc.audio_end() == 30000 - T.SESSION_GAP
    did = toc.musicbrainz_id()
    assert len(did) == 28 and did.endswith("-") and not set(did) - set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
    assert T.Toc([T.Track(1, 0), T.Track(2, 10001)], 50000).finish().musicbrainz_id() != did
    assert toc.musicbrainz_toc().split()[:3] == ["1", "2", str(30000 - T.SESSION_GAP + 150)]
    g = T.from_lengths([300, 400, 500], [150, 0])
    assert [(x.start, x.length) for x in g.tracks] == [(0, 300), (450, 400), (850, 500)] and g.leadout == 1350


def test_parse_windows_toc():
    def entry(ctl, num, lba):
        return bytes((0, ctl, num, 0)) + lba.to_bytes(4, "big", signed=True)
    raw = bytes((0, 34, 1, 3)) + entry(0x10, 1, 0) + entry(0x10, 2, 18000) + entry(0x14, 3, 40000) + \
        entry(0x10, 0xAA, 60000)
    toc = cdd.parse_toc(raw)
    assert [(x.number, x.start, x.audio) for x in toc.tracks] == [(1, 0, True), (2, 18000, True), (3, 40000, False)]
    assert toc.leadout == 60000 and toc.tracks[1].length == 40000 - T.SESSION_GAP - 18000


# ── CD-TEXT ───────────────────────────────────────────────────
def test_cdtext_packs():
    assert C.crc16(b"123456789") == 0x31C3
    assert C.translit("Кино — «Группа крови», № 1 Щука ёж") == 'Kino - "Gruppa krovi", No. 1 Shchuka yozh'
    long = "A very long title that takes more than one pack to hold"
    text = C.CdText({"title": "Album", "performer": "Band"},
                    [{"title": long, "performer": "Band"}, {"title": "Two"}, {"title": "Три"}]).spelled()
    packs = C.encode(text)
    assert all(C.pack_ok(p) for p in packs)
    assert packs[-1][0] == C.SIZE and packs[-3][0] == C.SIZE
    info = b"".join(p[4:16] for p in packs[-3:])
    assert info[1:3] == bytes((1, 3)) and info[4] == sum(1 for p in packs if p[0] == C.TITLE)
    back = C.decode(packs)
    assert back.album == {"title": "Album", "performer": "Band"}
    assert [d.get("title") for d in back.tracks] == [long, "Two", "Tri"]
    # read in any order, repeated, one pack damaged and repeated whole
    noisy = list(reversed(packs)) + packs
    bad = bytearray(packs[0])
    bad[5] ^= 0xFF
    assert C.decode([bytes(bad)] + noisy).tracks == back.tracks
    assert C.from_symbols(C.to_symbols(packs[2])) == packs[2]


# ── the raw image ─────────────────────────────────────────────
def test_image(tmp_path):
    toc = T.from_lengths([75 * 5, 75 * 3, 75 * 4], [150, 0])
    pcm = _pcm(toc.leadout)
    text = C.CdText({"title": "Album"}, [{"title": "One"}, {"title": "Two"}, {"title": "Three"}])
    path = str(tmp_path / "disc.raw")
    n = I.build(path, toc, lambda a, k: pcm[a * 2352:(a + k) * 2352], C.encode(text), leadout=300)
    assert n == -I.LEADIN_START + toc.leadout + 300 and os.path.getsize(path) == n * I.RAW
    r = I.read(path)
    assert r.bad_q == 0
    assert r.toc == [(1, 0, 2, 0), (2, 0, 9, 0), (3, 0, 0x12, 0), (0xA0, 1, 0, 0), (0xA1, 3, 0, 0), (0xA2, 0, 0x16, 0)]  # BCD
    assert r.tracks == {1: (-150, 0), 2: (375, 525), 3: (None, 750)} and r.leadout == toc.leadout
    assert C.decode(r.packs).tracks == [{"title": "One"}, {"title": "Two"}, {"title": "Three"}]
    i0 = -I.LEADIN_START
    p = r.pause
    assert not p[:i0 - 150].any() and p[i0 - 150:i0].all()                 # the 2 s before track 1
    assert p[i0 + 375:i0 + 525].all() and not p[i0:i0 + 375].any()        # track 2's pregap
    assert p[i0 + toc.leadout] == 1 and p[i0 + toc.leadout + 19] == 0      # the lead-out's flag
    assert I.read_audio(path, 0, toc.leadout) == pcm


def test_cue_bin(tmp_path):
    toc = T.from_lengths([300, 400], [75])
    pcm = _pcm(toc.leadout, 2)
    cue = str(tmp_path / "Альбом.cue")
    binp = I.write_cue(cue, toc, lambda a, k: pcm[a * 2352:(a + k) * 2352],
                       C.CdText({"title": "Альбом", "performer": "Группа"}, [{"title": "Раз"}, {"title": "Два"}]))
    assert open(binp, "rb").read() == pcm
    text = open(cue, encoding="utf-8").read()
    assert 'TITLE "Альбом"' in text and 'FILE "Альбом.bin" BINARY' in text
    assert "TRACK 02 AUDIO\n    TITLE \"Два\"\n    INDEX 00 00:04:00\n    INDEX 01 00:05:00" in text


# ── ripping ───────────────────────────────────────────────────
def _disc(n=(300, 200, 400), gaps=(150, 0), text=True, **kw):
    toc = T.from_lengths(list(n), list(gaps))
    pcm = _pcm(toc.leadout, 7)
    t = C.CdText({"title": "Album", "performer": "Band"}, [{"title": f"T{i + 1}"} for i in range(len(n))]) \
        if text else None
    return cdd.FakeDrive(pcm, toc, t, **kw), toc, pcm


def _float(pcm: bytes, a: int, b: int):
    return np.frombuffer(pcm[a * 2352:b * 2352], "<i2").reshape(-1, 2).astype(np.float32) / 32768.0


def test_rip_tracks_exact_and_offset(session):
    dr, toc, pcm = _disc(offset=30)
    meta = R.Meta.from_cdtext(dr.read_cdtext(), 3)
    assert meta.album == "Album" and meta.title(1) == "T2"
    docs, res = R.rip(dr, toc, [1, 3], meta, R.Options(secure=False, offset=30), session.folder(), "tracks")
    assert [d.name for d in docs] == ["01 - T1", "03 - T3"] and [r.number for r in res] == [1, 3]
    t3 = toc.track(3)
    assert np.array_equal(docs[1].read(0, docs[1].frames), _float(pcm, t3.start, t3.start + t3.length))
    import zlib
    assert res[1].crc == f"{zlib.crc32(pcm[t3.start * 2352:(t3.start + t3.length) * 2352]):08X}"
    # without the offset made up for: 30 frames late
    docs2, _r = R.rip(dr, toc, [1], meta, R.Options(secure=False, offset=0), session.folder(), "tracks")
    assert not np.array_equal(docs2[0].read(0, 100), _float(pcm, 0, 1)[:100])


def test_secure_rereads(session):
    dr, toc, pcm = _disc(flaky={10, 250}, flaky_times=1)
    docs, res = R.rip(dr, toc, [1], R.Meta(), R.Options(secure=True), session.folder(), "tracks")
    assert res[0].reread >= 1 and res[0].suspicious == 0
    assert np.array_equal(docs[0].read(0, docs[0].frames), _float(pcm, 0, 300))
    dr2, toc2, pcm2 = _disc(flaky={20}, flaky_times=1000)                  # a scratch that never reads the same
    _d, res2 = R.rip(dr2, toc2, [1], R.Meta(), R.Options(secure=True, retries=3), session.folder(), "tracks")
    assert res2[0].suspicious == 1
    _d, res3 = R.rip(*_disc(flaky={20}, flaky_times=1000)[:2], [1], R.Meta(), R.Options(secure=False),
                     session.folder(), "tracks")
    assert res3[0].reread == 0                                              # not secure: not even noticed


def test_rip_disc_with_regions_and_files(session, tmp_path):
    dr, toc, pcm = _disc()
    meta = R.Meta.from_cdtext(dr.read_cdtext(), 3)
    docs, _r = R.rip(dr, toc, [2, 3], meta, R.Options(secure=False), session.folder(), "disc")
    d = docs[0]
    assert d.name == "Album" and d.frames == (toc.track(3).start + 400 - toc.track(2).start) * SPF
    regs = [(m.name, m.pos, m.end) for m in d.markers]
    assert regs == [("T2", 0, 200 * SPF), ("T3", 200 * SPF, 600 * SPF)] and len(d.history) == 1
    saved = []

    def save(doc, i, number):
        p = str(tmp_path / R.name_for("{n:2} - {title}", meta, i, number)) + ".flac"
        R.save_track(doc, p, "flac", "16", R.tags_for(meta, i, number, 3))
        saved.append(p)
    docs, res = R.rip(dr, toc, [1, 2], meta, R.Options(), session.folder, "files", save=save)
    assert docs == [] and [os.path.basename(p) for p in saved] == ["01 - T1.flac", "02 - T2.flac"]
    from sound import media
    back = media.open_file(saved[1], session.folder())
    assert back.frames == 200 * SPF and back.info.tags.get("title") == "T2"
    assert back.info.tags.get("album") == "Album" and back.info.tags.get("track", "").startswith("2")
    x = back.read(0, back.frames)
    assert np.array_equal(x, _float(pcm, toc.track(2).start, toc.track(2).start + 200))   # FLAC 16: bit-exact


def test_musicbrainz_parse():
    data = {"releases": [
        {"title": "Группа крови", "date": "1988-01-05", "country": "SU",
         "artist-credit": [{"name": "Кино", "joinphrase": ""}],
         "media": [{"format": "CD", "discs": [{"id": "XYZ"}], "tracks": [
             {"position": 2, "title": "Закрой за мной дверь", "artist-credit": [{"name": "Кино"}]},
             {"position": 1, "title": "Группа крови", "artist-credit": [{"name": "Кино"}]},
             {"position": 3, "title": "Гость", "artist-credit": [{"name": "Кино", "joinphrase": " feat. "},
                                                                 {"name": "Гость"}]}]}]},
        {"title": "Other", "media": [{"discs": [], "tracks": [{"position": 1, "title": "x"}]}]}]}
    got = MB.parse(data, "XYZ", 3)
    assert len(got) == 1
    m = got[0]
    assert (m.album, m.artist, m.date) == ("Группа крови", "Кино", "1988")
    assert [x["title"] for x in m.tracks] == ["Группа крови", "Закрой за мной дверь", "Гость"]
    assert m.tracks[2]["artist"] == "Кино feat. Гость" and m.tracks[0]["artist"] == ""
    assert "MusicBrainz" not in MB.user_agent() and "ArtCoreSound/" in MB.user_agent()


# ── burning ───────────────────────────────────────────────────
def _doc(session, x, rate=44100):
    from sound.doc import Document
    d = Document(rate=rate, channels=2, folder=session.folder(), labels=("L", "R"), layout="stereo", name="mix.wav")
    d.set_initial(store.from_array(d.folder, np.asarray(x, np.float32)))
    return d


def test_tracks_of_and_plan(session):
    x = np.zeros((44100 * 30, 2), np.float32)
    d = _doc(session, x)
    d.add_region(44100 * 10, 44100 * 20, "Второй")
    d.add_region(0, 44100 * 5, "Первый")
    d.add_marker(44100 * 25, "Метка")
    assert [(s.a, s.b, s.title) for s in cdb.tracks_of(d, "regions")] == \
        [(0, 220500, "Первый"), (441000, 882000, "Второй")]
    assert [(s.a, s.b, s.title) for s in cdb.tracks_of(d, "markers")] == [(0, 1102500, ""), (1102500, 1323000,
                                                                                            "Метка")]
    assert [(s.a, s.b) for s in cdb.tracks_of(d, "whole")] == [(0, 1323000)]
    p = cdb.Plan(cdb.tracks_of(d, "regions"), gap=2.0, album="Альбом")
    assert p.toc().leadout == 375 + 150 + 750 and p.problems() == []
    assert p.text().album == {"title": "Albom"} and p.text().tracks[0] == {"title": "Pervyy"}
    p.tracks.append(cdb.TrackSpec(0, 44100))
    assert any("4 с" in s for s in p.problems())
    assert any("Не помещается" in s for s in cdb.Plan([cdb.TrackSpec(0, 44100 * 60 * 81)]).problems())


def test_burn_image_and_fake_recorder(session, tmp_path, monkeypatch):
    rng = np.random.default_rng(5)
    x = (rng.standard_normal((44100 * 14, 2)) * 0.2).astype(np.float32)
    d = _doc(session, x)
    d.add_region(0, 44100 * 5, "One")
    d.add_region(44100 * 6, 44100 * 13 + 100, "Two")                         # not a whole number of sectors
    plan = cdb.Plan(cdb.tracks_of(d, "regions"), gap=1.0, album="Test", performer="Me")
    toc, img = cdb.make_image(d, plan, session.folder())
    r = I.read(img)
    assert r.bad_q == 0 and r.tracks[2] == (toc.track(1).length, toc.track(2).start)
    assert C.decode(r.packs).album == {"title": "Test", "performer": "Me"}
    t2 = toc.track(2)
    got = np.frombuffer(I.read_audio(img, t2.start, t2.length), "<i2").reshape(-1, 2).astype(np.float64)
    want = x[44100 * 6:44100 * 13 + 100].astype(np.float64) * 32768
    assert np.abs(got[:len(want)] - want).max() <= 1.5                       # TPDF dither: within a step
    assert not got[len(want):].any() and t2.length == -(-(len(want)) // SPF)
    gap = I.read_audio(img, toc.track(1).length, 75)
    assert not any(gap)
    out = tmp_path / "burner"
    out.mkdir()
    monkeypatch.setenv("ARTCORE_FAKE_BURNER", str(out))
    rec = cdb.recorders()[0]
    assert cdb.media(rec).blank
    cdb.burn(rec, img, toc)
    assert open(out / "disc.raw", "rb").read() == open(img, "rb").read()
    toc2, pcm = cdb.prepare(d, plan, session.folder())
    files = cdb.split_tracks(pcm, toc2, session.folder())
    cdb.burn_tao(rec, files)
    assert sorted(os.listdir(out)) == ["disc.raw", "track01.pcm", "track02.pcm"]
    assert os.path.getsize(out / "track02.pcm") == t2.length * 2352


def test_burn_converts_rate(session):
    t = np.arange(48000 * 6) / 48000
    x = np.column_stack([0.3 * np.sin(2 * np.pi * 1000 * t)] * 2).astype(np.float32)
    d = _doc(session, x, rate=48000)
    plan = cdb.Plan([cdb.TrackSpec(0, 44100 * 6)])
    toc, pcm = cdb.prepare(d, plan, session.folder())
    y = np.frombuffer(open(pcm, "rb").read(), "<i2").reshape(-1, 2)[:44100 * 6].astype(np.float64) / 32768
    spec = np.abs(np.fft.rfft(y[44100:88200, 0]))
    assert abs(int(np.argmax(spec)) - 1000) <= 1 and 0.28 < np.abs(y[44100:88200]).max() < 0.32


# ── the windows ───────────────────────────────────────────────
def _fake_disc_files(tmp_path):
    files = []
    for i, (f, secs) in enumerate(((440, 4.2), (550, 5.0), (660, 4.6))):
        p = str(tmp_path / f"t{i + 1}.wav")
        ff("-f", "lavfi", "-i", f"sine=f={f}:d={secs}:r=44100", "-ac", "2", "-c:a", "pcm_s16le", p)
        files.append(os.path.basename(p))
    spec = {"files": files, "gaps": [150, 75], "album": {"title": "Фейк", "performer": "Группа"},
            "tracks": [{"title": "Первая"}, {"title": "Вторая"}, {"title": "Третья"}]}
    path = str(tmp_path / "disc.json")
    json.dump(spec, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    return path


def test_rip_window_and_burn_back(qapp, session, data_dir, tmp_path, monkeypatch):
    """Rip the (fake) disc into the editor as one file with regions, burn the regions back (fake recorder):
    the same tracks — and the same sound — on the new disc."""
    from sound import cdui
    from test_fx_ui import _close, _win
    monkeypatch.setenv("ARTCORE_FAKE_CD", _fake_disc_files(tmp_path))
    out = tmp_path / "burner"
    out.mkdir()
    monkeypatch.setenv("ARTCORE_FAKE_BURNER", str(out))
    w = _win(qapp, session)
    menus = {a.text(): a.menu() for a in w.menuBar().actions()}
    cd = next(a.menu() for a in menus["Файл"].actions() if a.text() == "Audio CD")
    assert [a.text() for a in cd.actions() if a.text()] == ["Рип Audio CD…", "Записать Audio CD…", "Извлечь диск"]
    dlg = cdui.RipDialog(w)
    dlg.wait()
    assert dlg.list.topLevelItemCount() == 3 and dlg.album.text() == "Фейк"
    assert [dlg.list.topLevelItem(i).text(2) for i in range(3)] == ["Первая", "Вторая", "Третья"]
    assert "CD-TEXT" in dlg.disc_l.text()
    dlg.list.topLevelItem(1).setText(2, "Вторая (правка)")
    dlg.mode.setCurrentIndex(dlg.mode.findData("disc"))
    s = dlg.settings()
    assert s["tracks"] == [1, 2, 3] and s["meta"].title(1) == "Вторая (правка)"
    dlg.accept()
    cdui.run_rip(w, s)
    assert pump(qapp, 60, lambda: w.job is None and len(w.docs) == 1)
    d = w.doc
    assert [m.name for m in d.markers] == ["Первая", "Вторая (правка)", "Третья"]
    src = cdd.FakeDrive.from_file(os.environ["ARTCORE_FAKE_CD"])
    bd = cdui.BurnDialog(w, d)
    bd.wait()
    assert bd.how.currentData() == "regions" and bd.list.topLevelItemCount() == 3
    assert "чистый" in bd.media_l.text() and bd.b_go.isEnabled()
    bd.gap.setValue(2.0)
    bs = bd.settings()
    bd.accept()
    cdui.run_burn(w, d, bs)
    assert pump(qapp, 120, lambda: w.job is None)
    r = I.read(str(out / "disc.raw"))
    burnt = C.decode(r.packs)
    assert [x["title"] for x in burnt.tracks] == ["Pervaya", "Vtoraya (pravka)", "Tretya"]
    assert burnt.album == {"title": "Feyk", "performer": "Gruppa"}
    new = T.from_lengths([t.length for t in src.toc.audio], [150, 150])
    for a, b in zip(src.toc.audio, new.audio):
        got = I.read_audio(str(out / "disc.raw"), b.start, b.length)
        assert got == src.pcm[a.start * 2352:(a.start + a.length) * 2352]          # 16-bit in, 16-bit out
    _close(w)


def test_rip_files_from_the_window(qapp, session, data_dir, tmp_path, monkeypatch):
    from sound import cdui
    from test_fx_ui import _close, _win
    monkeypatch.setenv("ARTCORE_FAKE_CD", _fake_disc_files(tmp_path))
    w = _win(qapp, session)
    dlg = cdui.RipDialog(w)
    dlg.wait()
    dlg.list.topLevelItem(2).setCheckState(0, cdui.Qt.Unchecked)
    dlg.mode.setCurrentIndex(dlg.mode.findData("files"))
    dlg.fmt.setCurrentIndex(dlg.fmt.findData("mp3"))
    dlg.folder.setText(str(tmp_path / "music"))
    s = dlg.settings()
    dlg.accept()
    assert w.cfg["cd_rip_format"] == "mp3" and w.cfg["cd_secure"] is True
    cdui.run_rip(w, s)
    assert pump(qapp, 60, lambda: w.job is None)
    results, saved = w.cd_last_rip
    names = sorted(os.path.relpath(p, tmp_path / "music") for p in saved)
    assert names == [os.path.join("Группа - Фейк", "01 - Первая.mp3"), os.path.join("Группа - Фейк", "02 - Вторая.mp3")]
    assert all(r.crc and not r.suspicious for r in results) and w.docs == []
    _close(w)


def test_burn_dialog_checks(qapp, session, data_dir, tmp_path, monkeypatch):
    from sound import cdui
    from test_fx_ui import _close, _win
    out = tmp_path / "burner"
    out.mkdir()
    monkeypatch.setenv("ARTCORE_FAKE_BURNER", str(out))
    w = _win(qapp, session)
    d = _doc(session, np.zeros((44100 * 3, 2), np.float32))
    w.add_doc(d)
    bd = cdui.BurnDialog(w, d)
    bd.wait()
    assert bd.how.count() == 1 and bd.how.currentData() == "whole"
    assert "короче 4 с" in bd.note.text() and not bd.b_go.isEnabled()
    monkeypatch.setattr(cdb, "media", lambda r: cdb.Media(True, False, True, 360000, "CD-RW"))
    bd.check_media()
    bd.wait()
    assert "сотрите" in bd.note.text() and bd.b_erase.isEnabled()
    bd.mode.setCurrentIndex(bd.mode.findData("tao"))
    assert not bd.plan().cdtext
    bd.reject()
    d.mark_saved()
    _close(w)
