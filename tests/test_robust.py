"""Robustness (1.0): a crashed run's unsaved documents come back (the process killed, the program started again),
unhandled errors are logged and shown, a full disk in the middle of an operation gives a clear message, leaves the
document as it was and removes the working files."""
import errno
import io
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time

import numpy as np
import pytest
from conftest import pump

from sound import batch, errors, media, proc, recovery, store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CRASHING_RUN = textwrap.dedent('''
    import os, sys, time
    sys.path.insert(0, {root!r})
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from sound import config, store
    from sound.window import MainWindow
    app = QApplication([])
    cfg = config.load()
    cfg.update(check_updates=False)
    s = store.Session(config.work_root(cfg))
    w = MainWindow(cfg, s)
    w.show()
    w.open_paths([{path!r}])
    t0 = time.time()
    while (not w.docs or w.job) and time.time() - t0 < 30:
        app.processEvents(); time.sleep(0.01)
    d = w.doc
    d.delete(0, d.rate // 2)
    d.replace(0, d.frames, __import__("sound.proc", fromlist=["x"]).apply_op(d, "gain", 0, d.frames, db=-6.0), "gain")
    d.add_marker(d.rate, "Метка")
    d.sel = (1000, 5000)
    w._edited()
    t0 = time.time()
    while time.time() - t0 < 4:
        app.processEvents(); time.sleep(0.02)
    print("READY", s.path, flush=True)
    while True:
        app.processEvents(); time.sleep(0.05)
''')


def test_crash_recovery_after_kill(qapp, media_dir, tmp_path, monkeypatch, data_dir):
    """The program is killed (SIGKILL) after three edits; the next start finds the document, restores the sound,
    the undo history and the markers; what is not taken is removed."""
    temp = os.path.join(str(tmp_path), "temp")
    env = dict(os.environ, ARTCORE_SOUND_TEMP=temp, ARTCORE_SOUND_DATA=os.path.join(str(tmp_path), "d"),
               QT_QPA_PLATFORM="offscreen", ARTCORE_AUDIO="null")
    src = os.path.join(media_dir, "stereo16.wav")
    script = os.path.join(str(tmp_path), "crash_run.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(CRASHING_RUN.format(root=ROOT, path=src))
    p = subprocess.Popen([sys.executable, script], env=env, stdout=subprocess.PIPE, text=True)
    line = ""
    t0 = time.time()
    while time.time() - t0 < 90:
        line = p.stdout.readline()
        if line.startswith("READY") or p.poll() is not None:
            break
    assert line.startswith("READY"), line
    old_session = line.split(None, 1)[1].strip()
    p.send_signal(signal.SIGKILL)
    p.wait(10)
    found = recovery.scan(temp)
    assert len(found) == 1
    e = found[0]
    assert e.name == "stereo16.wav" and e.steps == 3 and e.session == old_session
    # the expected document: the same edits on a fresh copy
    ref_s = store.Session(os.path.join(str(tmp_path), "ref"))
    ref = media.open_file(src, ref_s.folder())
    ref.delete(0, ref.rate // 2)
    ref.replace(0, ref.frames, proc.apply_op(ref, "gain", 0, ref.frames, db=-6.0), "gain")
    # a second, crashed run without unsaved changes is simply removed
    plain = os.path.join(temp, "s-0000000000")
    os.makedirs(os.path.join(plain, "d1"))
    s = store.Session(temp, clean=False)
    from sound import config
    from sound.window import MainWindow
    from sound import dialogs
    monkeypatch.setattr(dialogs.RecoveryDialog, "exec", lambda self: 1)
    w = MainWindow(config.load(), s)
    w.show()
    assert w.offer_recovery(found) == 1
    d = w.doc
    assert d.name == "stereo16.wav" and d.dirty and d.path == src
    assert d.frames == ref.frames and np.array_equal(d.read(0, d.frames), ref.read(0, ref.frames))
    assert [(m.name, m.pos) for m in d.markers] == [("Метка", d.rate)]
    assert d.sel == (1000, 5000)
    assert d.undo() and d.markers == ()
    assert d.undo() and d.undo() and d.frames == ref.frames + ref.rate // 2 and not d.can_undo()
    assert d.redo() and d.redo() and d.redo() and len(d.markers) == 1
    assert not os.path.isdir(plain)                              # the run with nothing to recover is gone
    assert os.path.isdir(old_session)                            # taken over by this session …
    w.write_journals()
    for x in w.docs:
        x.mark_saved()
    w.close()
    s.close()
    assert not os.path.isdir(old_session)                        # … and removed with it
    ref_s.close()


def test_journal_roundtrip_in_process(media_dir, tmp_path):
    s = store.Session(os.path.join(str(tmp_path), "w"))
    d = media.open_file(os.path.join(media_dir, "film.ac3"), s.folder())
    d.silence(1000, 3000)
    d.insert_silence(0, 480)
    d.add_region(100, 9000, "Регион")
    recovery.write(d, s.path)
    data = recovery.to_dict(d, s.path)
    assert data["dirty"] and data["channels"] == 6 and len(data["history"]) == 4
    assert all(not os.path.isabs(v.get("file", "x")) for v in data["sources"].values() if v["kind"] == "data")
    s2 = store.Session(os.path.join(str(tmp_path), "w2"))
    e = recovery.Entry(s.path, d.folder, data)
    back = recovery.restore(e, s2)
    assert back.frames == d.frames and np.array_equal(back.read(0, back.frames), d.read(0, d.frames))
    assert back.labels == d.labels and back.layout == d.layout and back.save_format == d.save_format
    mm = back.minmax(0, back.frames / 100, 100)
    assert np.allclose(np.nan_to_num(mm), np.nan_to_num(d.minmax(0, d.frames / 100, 100)))
    # a damaged working file: a clear error, not a broken document
    big = max((x for x in d.sources() if x.kind == "data"), key=lambda x: x.frames)
    with open(big.path, "r+b") as f:
        f.truncate(100)
    with pytest.raises(ValueError):
        recovery.restore(e, s2)
    s2.close()
    s.close()


def test_unhandled_exception_logged_and_shown(qapp, data_dir, monkeypatch):
    from sound import crash
    shown = []
    monkeypatch.setattr(crash, "show_dialog", lambda text, path: shown.append((text, path)))
    crash._bridge = None
    crash.install()
    crash.note("Нормализация…")

    def boom():
        raise ZeroDivisionError("в потоке")
    th = threading.Thread(target=boom, name="worker-1")
    th.start()
    th.join()
    assert pump(qapp, 3, lambda: shown)
    text, path = shown[0]
    assert os.path.isfile(path) and path.startswith(crash.crash_dir())
    log = open(path, encoding="utf-8").read()
    assert "ZeroDivisionError: в потоке" in log and "worker-1" in log and "Нормализация…" in log
    assert "ArtCore Sound" in log.splitlines()[0]
    dlg = crash.CrashDialog(text, path)
    dlg.copy()
    from PySide6.QtWidgets import QApplication
    assert "ZeroDivisionError" in QApplication.clipboard().text()
    dlg.close()
    sys.excepthook = sys.__excepthook__
    threading.excepthook = threading.__excepthook__


class _Quota:
    """open() for store.py that fails with ENOSPC once `left` bytes are written (across files)."""

    def __init__(self, left: int):
        self.left = left

    def __call__(self, path, mode="r", *a, **kw):
        f = open(path, mode, *a, **kw)
        if "w" not in mode:
            return f
        q = self

        class F(io.RawIOBase):
            def write(self, b):
                n = len(b)
                if q.left < n:
                    raise OSError(errno.ENOSPC, "No space left on device", path)
                q.left -= n
                return f.write(b)

            def close(self):
                f.close()
                super().close()
        return F()


def _working_files(folder):
    return sorted(n for n in os.listdir(folder) if ".f32" in n)


def test_disk_full_in_an_operation(media_dir, tmp_path, monkeypatch):
    s = store.Session(os.path.join(str(tmp_path), "w"))
    d = media.open_file(os.path.join(media_dir, "film.ac3"), s.folder())
    before = d.read(0, d.frames).copy()
    files, version, frames = _working_files(d.folder), d.version, d.frames
    monkeypatch.setattr(store, "open", _Quota(200_000), raising=False)
    with pytest.raises(OSError) as ex:
        d.replace(0, d.frames, proc.apply_op(d, "gain", 0, d.frames, db=-3.0), "gain")
    assert errors.is_disk_full(ex.value)
    msg = errors.human(ex.value)
    assert "места" in msg and "не изменён" in msg
    assert d.version == version and d.frames == frames and np.array_equal(d.read(0, d.frames), before)
    assert _working_files(d.folder) == files                    # the half-written source is removed
    # an effect (peaks written at the end) and a batch task too
    from sound import fx
    with pytest.raises(OSError):
        fx.render(d, 0, d.frames, fx.get("reverb"), {})
    assert _working_files(d.folder) == files
    out = os.path.join(str(tmp_path), "out.flac")
    job = batch.Job(steps=[batch.Step("gain", {"db": -1})])
    task = batch.Task(0, 1, os.path.join(media_dir, "film.ac3"), out=out, fmt="flac", option="24").to_dict()
    res = batch.run_task(task, job.to_dict(), s, threading.Event())
    assert res["status"] == "error" and "места" in res["error"] and not os.path.exists(out)
    assert [x for x in os.listdir(str(tmp_path)) if "artcore-part" in x] == []
    s.close()


def test_disk_full_in_the_window(qapp, session, media_dir, monkeypatch, data_dir):
    from PySide6.QtWidgets import QMessageBox
    from test_ui import _close, _open, _win
    w = _win(qapp, session)
    _open(qapp, w, os.path.join(media_dir, "stereo16.wav"))
    d = w.doc
    said = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: said.append(a[2]))
    monkeypatch.setattr(store, "open", _Quota(50_000), raising=False)
    w.apply_effect("comp", {})
    assert pump(qapp, 20, lambda: said and w.job is None)
    assert "места" in said[0] and not d.dirty and d.can_undo() is False
    monkeypatch.undo()
    _close(w)


@pytest.mark.skipif(not os.environ.get("ARTCORE_SMALL_DISK"), reason="needs a small disk (CI mounts an 8 MB tmpfs)")
def test_disk_full_on_a_real_small_disk(media_dir, tmp_path):
    """Saving to and opening on a disk that fills up: ffmpeg's «No space left» becomes the message, the target
    is not created, no temporary file stays; the working folder on a small disk is cleaned up too."""
    small = os.environ["ARTCORE_SMALL_DISK"]
    s = store.Session(os.path.join(str(tmp_path), "w"))
    from conftest import ff
    big = os.path.join(str(tmp_path), "big.wav")
    ff("-f", "lavfi", "-i", "anoisesrc=d=40:r=48000:a=0.3", "-ac", "6", "-c:a", "pcm_s24le", big)
    d = media.open_file(big, s.folder())
    target = os.path.join(small, "full.wav")
    with pytest.raises(RuntimeError) as ex:
        media.save(d, target, "wav", "24")
    assert errors.is_disk_full(ex.value) and "места" in errors.human(ex.value)
    assert not os.path.exists(target) and [n for n in os.listdir(small) if "artcore" in n] == []
    s2 = store.Session(os.path.join(small, "work"))
    with pytest.raises(OSError) as ex:
        media.open_file(big, s2.folder())
    assert errors.is_disk_full(ex.value)
    assert [f for _dp, _dn, fs in os.walk(s2.path) for f in fs if f != "lock"] == []
    s2.close()
    s.close()
