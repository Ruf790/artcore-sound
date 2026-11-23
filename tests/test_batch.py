"""Batch processing (1.0): a folder of real files made by ffmpeg through a chain, in worker processes — the loudness
target reached, the formats and names right, the subfolders kept, the existing-file policy, a broken file skipped
with its error in the log, Cancel leaving no half-written file, parallel = sequential, the presets to JSON and back,
the noise sample in a file, the window."""
import os
import shutil
import subprocess
import time

import numpy as np
import pytest
from conftest import ff, pump

from sound import batch, ff as sff, media


def _tree(media_dir, root):
    """in/a.wav, in/sub/b.flac, in/sub/deeper/c.mp3, in/sub/broken.wav, in/notes.txt."""
    src = os.path.join(root, "in")
    os.makedirs(os.path.join(src, "sub", "deeper"))
    shutil.copy(os.path.join(media_dir, "stereo16.wav"), os.path.join(src, "a.wav"))
    shutil.copy(os.path.join(media_dir, "hires.flac"), os.path.join(src, "sub", "b.flac"))
    shutil.copy(os.path.join(media_dir, "song.mp3"), os.path.join(src, "sub", "deeper", "c.mp3"))
    shutil.copy(os.path.join(media_dir, "broken.wav"), os.path.join(src, "sub", "broken.wav"))
    with open(os.path.join(src, "notes.txt"), "w") as f:
        f.write("not sound")
    return src


def _run(job, workers=2, timeout=240, **kw):
    r = batch.Runner(job, workers=workers, **kw)
    r.start()
    assert r.wait(timeout), "the batch did not finish"
    return r


def _lufs(path):
    from sound import analysis
    from sound.store import Session
    s = Session(os.path.join(os.path.dirname(path), ".m"))
    try:
        d = media.open_file(path, s.folder())
        return analysis.analyze(d).integrated, d
    finally:
        s.close()


def test_collect_and_names(media_dir, tmp_path):
    src = _tree(media_dir, str(tmp_path))
    found = batch.collect(batch.Source("folder", src, True))
    rels = [(os.path.basename(p), r) for p, r in found]
    assert rels == [("a.wav", ""), ("b.flac", "sub"), ("broken.wav", "sub"),
                    ("c.mp3", os.path.join("sub", "deeper"))]
    assert [os.path.basename(p) for p, _ in batch.collect(batch.Source("folder", src, False))] == ["a.wav"]
    only = batch.collect(batch.Source("folder", src, True, "flac, .MP3"))
    assert sorted(os.path.basename(p) for p, _ in only) == ["b.flac", "c.mp3"]
    assert batch.render_name("{n:3}_{name}-{date}", "Песня", 7, "2026-11-23") == "007_Песня-2026-11-23"
    assert batch.render_name("{name} {track} {lang} {other}", "x", 1, "d", 2, "") == "x 2 und {other}"
    assert batch.clean_name('a<b>:c"d/e\\f|g?h*. ') == "a_b__c_d_e_f_g_h_"
    p = os.path.join(str(tmp_path), "x.flac")
    open(p, "w").close()
    assert batch.unique_path(p, set()) == os.path.join(str(tmp_path), "x (2).flac")


def test_folder_batch_lufs_formats_structure_and_broken_file(media_dir, tmp_path):
    src = _tree(media_dir, str(tmp_path))
    out = os.path.join(str(tmp_path), "out")
    job = batch.Job(batch.Source("folder", src, True),
                    [batch.Step("normalize", {"mode": "loudness", "target": -16.0, "ceiling": -1.0}),
                     batch.Step("fades", {"fade_in": 20, "fade_out": 50}),
                     batch.Step("gain", {"db": -6.0}, enabled=False)],
                    batch.Output("flac", "16", "other", out, True, "{n:2}_{name}", ""))
    r = _run(job, workers=2)
    assert r.phase == "done" and not r.error
    by = {os.path.basename(x.src): x for x in r.tasks}
    assert by["broken.wav"].status == "error" and by["broken.wav"].error
    for name, rel, n in (("a.wav", "", 1), ("b.flac", "sub", 2), ("c.mp3", os.path.join("sub", "deeper"), 4)):
        x = by[name]
        assert x.status == "ok", (name, x.error)
        want = os.path.join(out, rel, f"{n:02d}_{os.path.splitext(name)[0]}.flac")
        assert x.out == want and os.path.isfile(want)
        info = sff.probe(want)
        assert info.tracks[0].codec == "flac" and info.tracks[0].bits == 16
        lufs, _d = _lufs(want)
        assert abs(lufs - -16.0) < 0.3, (name, lufs)
        assert abs(x.after["lufs"] - -16.0) < 0.1 and abs(x.before["lufs"] - -16.0) > 1
        assert x.seconds > 0 and x.duration > 1
    leftovers = [f for _dp, _dn, fs in os.walk(out) for f in fs if ".artcore-part" in f]
    assert leftovers == []
    snap = r.snapshot()
    assert snap["counts"]["ok"] == 3 and snap["counts"]["error"] == 1 and snap["frac"] == pytest.approx(1.0)
    # the report
    csvp, txtp = os.path.join(str(tmp_path), "r.csv"), os.path.join(str(tmp_path), "r.txt")
    batch.save_csv(r.tasks, csvp)
    batch.save_txt(r.tasks, txtp, 12.0, job)
    raw = open(csvp, "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf")                       # Excel reads the BOM as UTF-8
    import csv
    rows = list(csv.reader(open(csvp, encoding="utf-8-sig")))
    assert len(rows) == 5 and rows[1][7] and rows[1][8].startswith("-16")
    txt = open(txtp, encoding="utf-8").read()
    assert "broken.wav" in txt and "01_a.flac" in txt and "−" not in txt.split("\n")[0]


def test_existing_file_policy(media_dir, tmp_path):
    src = os.path.join(str(tmp_path), "in")
    os.makedirs(src)
    shutil.copy(os.path.join(media_dir, "mono24.wav"), os.path.join(src, "m.wav"))
    job = batch.Job(batch.Source("list", files=[os.path.join(src, "m.wav")]), [batch.Step("gain", {"db": -3.0})],
                    batch.Output("wav", "24", "same", "", True, "{name}", "_x", "skip"))
    r = _run(job, workers=1)
    target = os.path.join(src, "m_x.wav")
    assert r.tasks[0].status == "ok" and os.path.isfile(target)
    mtime = os.path.getmtime(target)
    time.sleep(0.05)
    r = _run(job, workers=1)
    assert r.tasks[0].status == "skip" and os.path.getmtime(target) == mtime
    job.output.exists = "rename"
    r = _run(job, workers=1)
    assert r.tasks[0].status == "ok" and r.tasks[0].out == os.path.join(src, "m_x (2).wav")
    job.output.exists = "replace"
    job.steps = [batch.Step("gain", {"db": -12.0})]
    r = _run(job, workers=1)
    assert r.tasks[0].out == target and os.path.getmtime(target) > mtime
    names = sorted(os.listdir(src))
    assert names == ["m.wav", "m_x (2).wav", "m_x.wav"]


def test_two_tasks_one_name_and_video_tracks(media_dir, tmp_path):
    """Two files that would get the same name both get one; a film's tracks each to their own file."""
    a = os.path.join(str(tmp_path), "a")
    b = os.path.join(str(tmp_path), "b")
    os.makedirs(a)
    os.makedirs(b)
    shutil.copy(os.path.join(media_dir, "mono24.wav"), os.path.join(a, "x.wav"))
    shutil.copy(os.path.join(media_dir, "stereo16.wav"), os.path.join(b, "x.wav"))
    film = os.path.join(media_dir, "Фильм (2024).mkv")
    out = os.path.join(str(tmp_path), "out")
    job = batch.Job(batch.Source("list", files=[os.path.join(a, "x.wav"), os.path.join(b, "x.wav"), film],
                                 tracks="all"), [], batch.Output("source", "", "other", out, True, "{name}", ""))
    tasks = batch.plan(job)
    outs = [os.path.basename(x.out) for x in tasks]
    assert outs == ["x.wav", "x (2).wav", "Фильм (2024)_a1.flac", "Фильм (2024)_a2.flac"]
    assert tasks[0].fmt == "wav" and tasks[0].option == "24" and tasks[1].option == "16"
    job.source.tracks = "lang"
    job.source.lang = "eng"
    t = batch.plan(job)[-1]
    assert t.track == 1 and "eng" in t.track_text
    job.source.lang = "deu"
    t = batch.plan(job)[-1]
    assert t.track == 0 and t.notes                                # the default track, with a note


def test_parallel_equals_sequential(media_dir, tmp_path):
    src = _tree(media_dir, str(tmp_path))
    os.remove(os.path.join(src, "sub", "broken.wav"))
    steps = [batch.Step("dc"), batch.Step("comp"), batch.Step("eq", {"b3_on": True, "b3_gain": 4.0}),
             batch.Step("reverb", {"mix": 0.3}), batch.Step("limit"), batch.Step("rate", {"rate": 48000})]
    outs = {}
    for n in (1, 3):
        out = os.path.join(str(tmp_path), f"out{n}")
        job = batch.Job(batch.Source("folder", src, True), steps, batch.Output("wav", "32f", "other", out, False),
                        measure=False)
        r = _run(job, workers=n)
        assert all(x.status == "ok" for x in r.tasks), [x.error for x in r.tasks]
        outs[n] = {os.path.basename(x.out): open(x.out, "rb").read() for x in r.tasks}
    assert outs[1].keys() == outs[3].keys() and len(outs[1]) == 3
    for k in outs[1]:
        assert outs[1][k] == outs[3][k], k


def test_cancel_leaves_no_partial_files(tmp_path):
    src = os.path.join(str(tmp_path), "in")
    os.makedirs(src)
    for i in range(3):
        ff("-f", "lavfi", "-i", f"anoisesrc=d=90:c=pink:r=48000:a=0.2:seed={i}", "-filter_complex",
           "[0]pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0", "-c:a", "pcm_s16le", os.path.join(src, f"n{i}.wav"))
    out = os.path.join(str(tmp_path), "out")
    work = os.path.join(str(tmp_path), "work")
    job = batch.Job(batch.Source("folder", src), [batch.Step("declick"), batch.Step("reverb")],
                    batch.Output("flac", "", "other", out))
    r = batch.Runner(job, work, workers=2)
    r.start()
    t0 = time.time()
    while time.time() - t0 < 60 and not any(x.frac > 0.3 for x in r.snapshot()["tasks"]):
        time.sleep(0.05)
    r.cancel()
    assert r.wait(60)
    assert r.phase == "cancelled"
    st = [x.status for x in r.tasks]
    assert "cancel" in st and "ok" not in st
    found = [f for _dp, _dn, fs in os.walk(out) for f in fs] if os.path.isdir(out) else []
    assert found == []                                           # no result and no .artcore-part
    assert [n for n in os.listdir(work) if n.startswith("s-")] == []   # the workers' folders are gone


def test_disk_space_is_reserved(media_dir, tmp_path):
    src = _tree(media_dir, str(tmp_path))
    job = batch.Job(batch.Source("folder", src), [batch.Step("gain")],
                    batch.Output("wav", "", "other", os.path.join(str(tmp_path), "out")))
    r = batch.Runner(job, workers=2)
    r.free_space = lambda _p: 10 << 20                           # 10 MB: less than any task reserves
    r.start()
    assert r.wait(60)
    errs = [x for x in r.tasks if x.status == "error" and "broken" not in x.src]
    assert len(errs) == 3 and all("места" in x.error or "space" in x.error for x in errs)


def test_presets_roundtrip(tmp_path):
    steps = [batch.Step(k) for k in batch.STEP_KINDS]
    steps[1] = batch.Step("normalize", {"mode": "true_peak", "target": -2.5, "ceiling": -0.5})
    steps[-2] = batch.Step("nr", {"reduction": 20.0, "profile": "C:\\noise\\hum.acnoise"})
    steps[3].enabled = False
    job = batch.Job(batch.Source("list", files=["C:\\Музыка\\a.flac"], tracks="lang", lang="rus, ru"), steps,
                    batch.Output("opus", "256", "other", "D:\\out", False, "{n:3} {name}", "-v2", "replace", False,
                                 False), 3, False)
    p = os.path.join(str(tmp_path), "Мой пакет" + batch.PRESET_EXT)
    job.save(p)
    back = batch.Job.load(p)
    assert back.to_dict() == job.to_dict()
    assert back.steps[1].values == {"mode": "true_peak", "target": -2.5, "ceiling": -0.5}
    assert back.steps[-2].values["profile"] == "C:\\noise\\hum.acnoise" and not back.steps[3].enabled
    # unknown steps and bad values do not break the loading
    d = job.to_dict()
    d["steps"].append({"kind": "from_the_future", "values": {}})
    d["steps"][0]["values"]["db"] = 1e9
    j2 = batch.Job.from_dict(d)
    assert len(j2.steps) == len(steps) and j2.steps[0].values["db"] == 40.0
    with pytest.raises(ValueError):
        batch.Job.from_dict({"kind": "something else"})


def test_noise_profile_file_and_batch_nr(tmp_path):
    from sound import fx
    from sound.store import Session
    src = os.path.join(str(tmp_path), "in")
    os.makedirs(src)
    ff("-f", "lavfi", "-i", "anoisesrc=d=6:c=white:r=44100:a=0.05:seed=3", "-f", "lavfi", "-i",
       "sine=f=440:d=6:r=44100", "-filter_complex", "[1]volume=0.5,aeval='val(0)*gt(t,2)'[s];[0][s]amix=2:normalize=0",
       "-ac", "1", "-c:a", "pcm_s16le", os.path.join(src, "talk.wav"))
    s = Session(os.path.join(str(tmp_path), "w"))
    d = media.open_file(os.path.join(src, "talk.wav"), s.folder())
    prof = fx.take_noise_profile(d, 0, int(1.8 * d.rate))
    pp = os.path.join(str(tmp_path), "hiss" + batch.NOISE_EXT)
    batch.save_noise_profile(pp, prof)
    back = batch.load_noise_profile(pp)
    assert back["rate"] == 44100 and np.allclose(back["power"], prof["power"])
    other = batch.adapt_noise_profile(back, 48000)
    assert other["n"] == fx.restore.frame_size(48000) and other["power"].shape[0] == other["n"] // 2 + 1
    s.close()
    out = os.path.join(str(tmp_path), "out")
    job = batch.Job(batch.Source("folder", src), [batch.Step("nr", {"reduction": 24.0, "profile": pp})],
                    batch.Output("wav", "32f", "other", out))
    r = _run(job, workers=1)
    x = r.tasks[0]
    assert x.status == "ok", x.error
    assert x.after["lufs"] < x.before["lufs"]                    # the hiss is gone, the tone stays
    job.steps[0].values["profile"] = ""
    r = _run(job, workers=1)
    assert r.tasks[0].status == "error" and "acnoise" in r.tasks[0].error


def test_batch_window(qapp, session, media_dir, tmp_path, data_dir):
    from sound import config
    from sound.batchui import BatchWindow
    from sound.window import MainWindow
    cfg = config.load()
    cfg.update(check_updates=False, geometry="")
    w = MainWindow(cfg, session)
    w.show()
    src = _tree(media_dir, str(tmp_path))
    w.batch_window()
    bw = w.batch_win
    assert isinstance(bw, BatchWindow) and bw.isVisible()
    out = os.path.join(str(tmp_path), "out")
    job = batch.Job(batch.Source("folder", src, True), [batch.Step("normalize"), batch.Step("limit")],
                    batch.Output("mp3", "192", "other", out, True, "{name}", "_n"))
    bw.set_job(job)
    assert bw.job().to_dict() == job.to_dict()
    pump(qapp, 0.6)
    assert "4" in bw.src_count.text()
    bw.save_preset("Тест")
    assert "Тест" in batch.list_presets()
    bw.set_job(batch.Job())
    bw.presets.setCurrentIndex(bw.presets.findData("Тест"))
    bw._preset_chosen(0)
    assert bw.job().to_dict() == job.to_dict()
    # the effect window of a step: values in, values out, the preview on the first file
    d, name = bw._preview_doc()
    assert name == "a.wav" and 0 < d.frames <= 30 * d.rate
    bw.chain.setCurrentRow(1)
    bw.start()
    assert pump(qapp, 180, lambda: not bw._running() and bw.runner.phase == "done")
    pump(qapp, 0.4)
    assert bw.table.rowCount() == 4
    statuses = [bw.table.item(i, 2).text() for i in range(4)]
    assert statuses.count("готово") == 3 and statuses.count("ошибка") == 1
    assert os.path.isfile(os.path.join(out, "sub", "deeper", "c_n.mp3"))
    assert bw.b_report.isEnabled() and bw.b_open_out.isEnabled()
    rp = os.path.join(str(tmp_path), "rep.txt")
    bw.save_report(rp, "txt")
    assert "c_n.mp3" in open(rp, encoding="utf-8").read()
    bw.close()
    assert w.cfg["batch_last"]["output"]["suffix"] == "_n"
    w.close()


def test_batch_from_the_command_line_spawn_guard():
    """The frozen exe calls freeze_support() first (a worker must not open a window)."""
    run = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "run.py"), encoding="utf-8").read()
    assert "freeze_support()" in run
    assert subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0
