"""Macros (2.0, block 3): the steps (editing, processing, effects, saving) played back on a document — selections
in seconds from the start or from the end, so one macro fits files of any length; recording from what is done in
the window (a changed selection becomes a «select» step, Undo takes the last command back); kept as files with a
hotkey; run in the window on a copy, as one undo step (the file untouched when a step fails); a batch step."""
import json
import os

import numpy as np
import pytest
from conftest import ff, pump

from sound import batch, config, macro, store
from sound.doc import Document

RATE = 48000


def _doc(session, x, path=""):
    from sound.ff import labels
    x = np.asarray(x, np.float32)
    d = Document(rate=RATE, channels=x.shape[1], folder=session.folder(), labels=tuple(labels(x.shape[1])),
                 layout={1: "mono", 2: "stereo", 6: "5.1"}.get(x.shape[1], ""), path=path,
                 name=os.path.basename(path))
    d.set_initial(store.from_array(d.folder, x))
    return d


def _ramp(n, ch=2):
    return np.tile((np.arange(n, dtype=np.float32) % 1000 / 2000.0 + 0.1)[:, None], (1, ch))


def S(kind, **v):
    return macro.Step(kind, v)


# ── the steps ─────────────────────────────────────────────────
def test_select_from_the_end_fits_any_length(session):
    steps = [S("select", a=1.0, a_from="end", b=0.0, b_from="end"), S("fade_out", curve="linear"),
             S("select", a=0.0, a_from="start", b=0.5, b_from="start"), S("delete")]
    for secs in (3, 7):
        x = _ramp(secs * RATE)
        d = _doc(session, x)
        macro.run(d, steps)
        assert d.frames == secs * RATE - RATE // 2
        y = d.read(0, d.frames)
        assert np.array_equal(y[:d.frames - RATE], x[RATE // 2:secs * RATE - RATE])     # the middle as it was
        assert abs(y[-1, 0]) < 1e-3 and y[-RATE, 0] == pytest.approx(x[-RATE, 0], abs=1e-3)   # faded out
        assert d.sel is None


def test_cursor_copy_paste_silence_trim(session):
    x = _ramp(4 * RATE)
    d = _doc(session, x)
    ctx = macro.run(d, [S("select", a=0.0, a_from="start", b=1.0, b_from="start"), S("copy"),
                        S("select", a=0.0, a_from="end", b=0.0, b_from="end"), S("paste"),        # at the end
                        S("select", a=2.0, a_from="start", b=2.0, b_from="start"), S("insert_silence", seconds=0.25)])
    assert d.frames == 5 * RATE + RATE // 4 and ctx.clip is not None
    y = d.read(0, d.frames)
    assert np.array_equal(y[-RATE:], x[:RATE]) and not np.any(y[2 * RATE:2 * RATE + RATE // 4])
    macro.run(d, [S("select", a=1.0, a_from="start", b=2.0, b_from="start"), S("silence"), S("trim")])
    assert d.frames == RATE and not np.any(d.read(0, d.frames))
    ctx = macro.run(d, [S("select_none"), S("paste")])                                   # nothing to paste
    assert ctx.notes and "буфер пуст" in ctx.notes[0]


def test_processing_on_the_selection_and_the_whole(session):
    x = _ramp(2 * RATE)
    d = _doc(session, x)
    macro.run(d, [S("select", a=0.0, a_from="start", b=1.0, b_from="start"), S("gain", db=-6.0),
                  S("select_none"), S("reverse"), S("channels", channels=1, method="auto")])
    assert d.channels == 1 and d.frames == 2 * RATE
    y = d.read(0, d.frames)[:, 0] / np.sqrt(2)                       # the mono mix: (L + R) / √2
    g = 10 ** (-6 / 20)
    assert np.allclose(y[RATE:], x[:RATE, 0][::-1] * g, atol=1e-5)                   # reversed after the gain
    assert np.allclose(y[:RATE], x[RATE:, 0][::-1], atol=1e-5)


def test_effects_and_normalize(session):
    t = np.arange(2 * RATE) / RATE
    x = np.column_stack([0.1 * np.sin(2 * np.pi * 440 * t)] * 2).astype(np.float32)
    d = _doc(session, x)
    ctx = macro.run(d, [S("normalize", mode="peak", target=-3.0, ceiling=-1.0),
                        S("eq", b4_gain=0.0), S("fades", fade_in=100.0, fade_out=0.0, curve="linear")])
    y = d.read(0, d.frames)
    assert float(np.abs(y[RATE:]).max()) == pytest.approx(10 ** (-3 / 20), abs=2e-3)
    assert abs(y[0, 0]) < 1e-6 and not ctx.notes


def test_save_step(session, tmp_path):
    src = str(tmp_path / "song.wav")
    d = _doc(session, _ramp(RATE), path=src)
    out = tmp_path / "out"
    ctx = macro.run(d, [S("save", format="flac", template="{name}-clean", folder=str(out)),
                        S("save", format="wav")])
    assert ctx.saved == [str(out / "song-clean.flac"), src]
    from sound import ff as ffm
    assert ffm.probe(ctx.saved[0]).tracks[0].channels == 2


def test_steps_clean_and_files(data_dir):
    s = macro.Step("select", {"a": -5, "a_from": "middle", "b": 2})
    assert s.values == {"a": 0.0, "a_from": "start", "b": 2.0, "b_from": "start"}
    with pytest.raises(ValueError):
        macro.Step("format_disk")
    m = macro.Macro("Чистка", [S("dc"), S("select", a=1.0, a_from="end", b=0.0, b_from="end"),
                               S("fade_out", curve="log"), macro.Step("vst3", {"plugin": {"name": "X", "path": "x"}})],
                    "Ctrl+Alt+1")
    path = macro.save(m)
    assert os.path.basename(path) == "Чистка.acmacro"
    back = macro.load(path)
    assert back.name == "Чистка" and back.hotkey == "Ctrl+Alt+1" and len(back.steps) == 4
    assert back.steps[2].values == {"curve": "log"} and back.steps[3].values["plugin"]["name"] == "X"
    # a step of a newer version is left out, the rest kept
    data = json.load(open(path, encoding="utf-8"))
    data["steps"].insert(0, {"kind": "time_travel", "values": {}})
    json.dump(data, open(path, "w", encoding="utf-8"))
    assert len(macro.load(path).steps) == 4
    m2 = macro.load(path)
    m2.name = "Чистка 2"
    macro.save(m2)                                       # renamed: the old file goes
    assert [x.name for x in macro.load_all()] == ["Чистка 2"]
    macro.delete(m2)
    assert macro.load_all() == []
    assert S("select", a=1.5, a_from="end", b=0.0, b_from="end").summary() == "1.5 с до конца … 0 с до конца"


# ── recording ─────────────────────────────────────────────────
def test_recorder_selection_steps(session):
    d = _doc(session, _ramp(10 * RATE))
    r = macro.Recorder()
    d.sel = (0, RATE)
    r.note(d, "gain", {"db": -3.0})
    r.note(d, "eq", {})                                  # the same selection: no new «select»
    d.sel = (9 * RATE, 10 * RATE)                        # near the end: counted from it
    r.note(d, "fade_out", {"curve": "linear"})
    d.sel = None
    d.cursor = 5 * RATE
    r.note(d, "insert_silence", {"seconds": 1.0}, before=r.place(d, cursor=True))
    r.note(d, "save", {"format": "wav"})
    kinds = [s.kind for s in r.steps]
    assert kinds == ["select", "gain", "eq", "select", "fade_out", "select", "insert_silence", "save"]
    assert r.steps[0].values == {"a": 0.0, "a_from": "start", "b": 1.0, "b_from": "start"}
    assert r.steps[3].values == {"a": 1.0, "a_from": "end", "b": 0.0, "b_from": "end"}
    assert r.steps[5].values["a"] == r.steps[5].values["b"] == 5.0
    r.undo()
    r.undo()
    assert [s.kind for s in r.steps][-1] == "fade_out"
    d.sel = (9 * RATE, 10 * RATE)
    r.note(d, "reverse")
    assert [s.kind for s in r.steps][-2:] == ["select", "reverse"]   # after Undo the place is recorded again


# ── the window ────────────────────────────────────────────────
def _tone(tmp_path, name, secs, f=440):
    p = str(tmp_path / name)
    ff("-f", "lavfi", "-i", f"sine=f={f}:d={secs}:r=48000", "-af", "aformat=channel_layouts=stereo,volume=0.25",
       "-c:a", "pcm_f32le", p)
    return p


def test_record_in_the_window_and_run(qapp, session, data_dir, tmp_path):
    from test_fx_ui import _close, _open, _win
    w = _win(qapp, session)
    _open(qapp, w, _tone(tmp_path, "a.wav", 4))
    d = w.doc
    menus = {a.text(): a.menu() for a in w.menuBar().actions()}
    acts = [a.text() for a in menus["Макросы"].actions() if a.text()]
    assert acts[:2] == ["Записать макрос", "Управление макросами…"] and "Макросов нет — «Записать макрос»" in acts
    w.macro_record_toggle()
    assert w.mrec is not None and w.st_macro.isVisibleTo(w) and w.a_mrec.text() == "Остановить запись макроса…"
    d.sel = (0, RATE // 2)                                    # cut the first half second
    w._on_selection()
    w.delete()
    d.sel = (d.frames - RATE, d.frames)                       # the last second −6 dB
    w._on_selection()
    w.simple_op("gain", db=-6.0)
    assert pump(qapp, 20, lambda: w.job is None and len(d.history) == 3)
    w.simple_op("reverse")                                    # then taken back
    assert pump(qapp, 20, lambda: w.job is None and len(d.history) == 4)
    w.undo()
    d.sel = None
    w._on_selection()
    w.apply_effect("eq", {"b4_gain": 0.0})
    assert pump(qapp, 20, lambda: w.job is None)
    rec = w.mrec
    kinds = [s.kind for s in rec.steps]
    assert kinds == ["select", "delete", "select", "gain", "select_none", "eq"]
    assert "шагов 6" in w.st_macro.text()
    w.mrec = None                                             # stopped (the name asked by the dialog: given here)
    w.save_recorded(rec, "Подрезать")
    assert [a.text() for a in w._macro_actions] == ["Подрезать"]
    # on another file of another length: the same cut, the last second of it
    _open(qapp, w, _tone(tmp_path, "b.wav", 6, 330))
    d2 = w.doc
    x = d2.read(0, d2.frames).astype(np.float64)
    m = macro.find("Подрезать")
    w.run_macro(m)
    assert pump(qapp, 30, lambda: w.job is None)
    assert d2.frames == 6 * RATE - RATE // 2 and len(d2.history) == 2
    assert d2.history[-1].label == "Макрос: Подрезать"
    y = d2.read(0, d2.frames)
    assert np.allclose(y[:-RATE], x[RATE // 2:-RATE], atol=1e-6)
    assert np.allclose(y[-RATE:], x[-RATE:] * 10 ** (-6 / 20), atol=1e-6)
    w.undo()
    assert d2.frames == 6 * RATE
    _close(w)


def test_failed_macro_leaves_the_file(qapp, session, data_dir, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from test_fx_ui import _close, _open, _win
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    w = _win(qapp, session)
    _open(qapp, w, _tone(tmp_path, "a.wav", 2))
    d = w.doc
    x = d.read(0, d.frames)
    m = macro.Macro("Плохой", [S("gain", db=-6.0), macro.Step("nr", {"profile": ""})])
    w.run_macro(m)
    assert pump(qapp, 20, lambda: w.job is None)
    assert len(d.history) == 1 and np.array_equal(d.read(0, d.frames), x)
    assert warned and "Плохой" in warned[0] and "не изменён" in warned[0]
    _close(w)


def test_hotkeys(qapp, session, data_dir):
    from test_fx_ui import _close, _win
    macro.save(macro.Macro("Один", [S("dc")], "Ctrl+Alt+1"))
    macro.save(macro.Macro("Чужая", [S("dc")], "Ctrl+B"))       # the batch's key: not given to the macro
    w = _win(qapp, session)
    by = {a.text(): a for a in w._macro_actions}
    assert by["Один"].shortcut().toString() == "Ctrl+Alt+1"
    assert by["Чужая"].shortcut().isEmpty()
    from sound import macroui
    dlg = macroui.MacroDialog(w, macro.find("Один"))
    from PySide6.QtGui import QKeySequence
    dlg.key.setKeySequence(QKeySequence("Ctrl+B"))
    assert not dlg._check_key() and "занята" in dlg.key_note.text()
    dlg.key.setKeySequence(QKeySequence("Ctrl+Alt+2"))
    assert dlg._check_key()
    _close(w)


def test_manager_and_dialog(qapp, session, data_dir, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    from sound import macroui
    from test_fx_ui import _close, _win
    macro.save(macro.Macro("Тест", [S("dc"), S("select_none"), S("gain", db=2.0)]))
    w = _win(qapp, session)
    mg = macroui.MacroManager(w)
    assert mg.list.count() == 1 and "шагов: 3" in mg.list.item(0).text()
    mg._duplicate()
    assert [m.name for m in macro.load_all()] == ["Тест", "Тест (копия)"]
    out = str(tmp_path / "t.acmacro")
    mg.list.setCurrentRow(0)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (out, ""))
    mg._export()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    mg._delete()
    assert [m.name for m in macro.load_all()] == ["Тест (копия)"]
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (out, ""))
    mg._import()
    assert sorted(m.name for m in macro.load_all()) == ["Тест", "Тест (копия)"]
    dlg = macroui.MacroDialog(w, macro.find("Тест"))
    assert dlg.list.count() == 3
    dlg.list.setCurrentRow(0)
    dlg._move(1)
    dlg.list.item(2).setCheckState(macroui.Qt.Unchecked)
    r = dlg.result()
    assert [s.kind for s in r.steps] == ["select_none", "dc", "gain"] and not r.steps[2].enabled
    _close(w)


# ── the batch ─────────────────────────────────────────────────
def test_batch_step(session, tmp_path):
    m = macro.Macro("M", [S("select", a=0.0, a_from="start", b=1.0, b_from="start"), S("delete"),
                          S("save", format="flac", template="{name}-m")])
    s = batch.Step("macro", {"name": m.name, "steps": [x.to_dict() for x in m.steps]})
    assert s.title() == "Макрос: M" and s.summary() == "шагов: 3"
    assert batch.Step.from_dict(json.loads(json.dumps(s.to_dict()))).values == s.values
    src = str(tmp_path / "file.wav")
    d = _doc(session, _ramp(3 * RATE), path=src)
    notes = []
    batch.apply_step(d, s, lambda f: None, notes, source=src)
    assert d.frames == 2 * RATE and len(d.history) == 1
    assert os.path.exists(str(tmp_path / "file-m.flac")) and "file-m.flac" in notes[-1]
    bad = batch.Step("macro", {"name": "B", "steps": [{"kind": "nr", "values": {}}]})
    with pytest.raises(RuntimeError, match="Макрос «B»"):
        batch.apply_step(d, bad, lambda f: None)
    nested = batch.Step("macro", {"name": "N", "steps": [{"kind": "macro", "values": {}}, {"kind": "dc"}]})
    assert [x["kind"] for x in nested.values["steps"]] == ["dc"]


def test_batch_runs_a_macro_in_workers(session, tmp_path, data_dir):
    files = []
    for i in range(2):
        p = str(tmp_path / f"in{i}.wav")
        ff("-f", "lavfi", "-i", f"sine=f={300 + 100 * i}:d={2 + i}:r=48000", "-ac", "2", "-c:a", "pcm_f32le", p)
        files.append(p)
    m = [S("select", a=0.5, a_from="end", b=0.0, b_from="end"), S("delete")]
    job = batch.Job(source=batch.Source(mode="list", files=files),
                    steps=[batch.Step("macro", {"name": "cut", "steps": [x.to_dict() for x in m]})],
                    output=batch.Output(format="wav", folder_mode="other", folder=str(tmp_path / "out"), suffix=""))
    r = batch.Runner(job, str(tmp_path / "work"), workers=2)
    r.start()
    assert r.wait(120)
    assert all(x.status == "ok" for x in r.tasks), [(x.status, x.error) for x in r.tasks]
    from sound import ff as ffm
    for i, p in enumerate(files):
        out = str(tmp_path / "out" / os.path.basename(p))
        assert ffm.probe(out).duration == pytest.approx(2 + i - 0.5, abs=0.01)
