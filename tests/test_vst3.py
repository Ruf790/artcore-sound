"""VST3 plugins (2.0, block 3): the host process (the sound through it unchanged in length, a plugin's latency made
up for, channels in groups — all, pairs, each — and those not used passed through, lined up), a plugin that crashes or
hangs does not take the program down, the effect over a document (render, the tail), scanning (the plugins of a
bundle, the cache, those that crash while scanned), the effect window (parameters, presets with the plugin's state,
.vstpreset files, the preview, Apply), the batch step, the menu.

The plugin is ArtCore Test Plug (tools/testplug, built from the VST3 SDK): ARTCORE_TESTPLUG is its .vst3, else
build/testplug/VST3/Release/ArtCoreTestPlug.vst3 of the repository."""
import os
import shutil

import numpy as np
import pytest
from conftest import pump

from sound import batch, config, fx, store
from sound.doc import Document

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAIN, LATE = "ArtCore Test Plug", "ArtCore Test Plug Latency"
RATE = 48000


def _plug_path() -> str:
    for p in (os.environ.get("ARTCORE_TESTPLUG", ""),
              os.path.join(ROOT, "build", "testplug", "VST3", "Release", "ArtCoreTestPlug.vst3")):
        if p and os.path.exists(p):
            return p
    return ""


@pytest.fixture(scope="session")
def plug():
    pytest.importorskip("pedalboard")
    p = _plug_path()
    if not p:
        pytest.skip("ArtCore Test Plug is not built (tools/testplug)")
    return p


def _ref(path, name=PLAIN):
    return {"path": path, "name": name, "id": "", "maker": "ArtCore"}


def _doc(session, x, rate=RATE):
    from sound.ff import labels
    x = np.asarray(x, np.float32)
    d = Document(rate=rate, channels=x.shape[1], folder=session.folder(), labels=tuple(labels(x.shape[1])),
                 layout={1: "mono", 2: "stereo", 6: "5.1"}.get(x.shape[1], ""))
    d.set_initial(store.from_array(d.folder, x))
    return d


def _noise(n, ch, seed=0):
    return (np.random.default_rng(seed).standard_normal((n, ch)) * 0.1).astype(np.float32)


GAIN_6DB = 30.0 / 48.0                      # Gain: −24…+24 dB on 0…1


# ── the host ──────────────────────────────────────────────────
def test_host_latency_and_channels_passed_through(plug):
    from sound.vst.client import Host
    h = Host()
    try:
        info = h.load(plug, LATE, RATE, 6, "auto", [0, 1, 2, 4, 5])          # LFE not used
        assert info["latency"] == 1000 and info["mode"] == "all" and info["groups"] == [[0, 1, 2, 4, 5]]
        assert {p["key"] for p in info["params"]} == {"gain_db", "crash", "hang"}
        x = _noise(RATE, 6)
        y = np.concatenate([h.process(x[i:i + 3000]) for i in range(0, len(x), 3000)])   # odd blocks
        assert y.shape == x.shape
        assert np.array_equal(y[1000:], x[:-1000]) and not np.any(y[:1000])   # all lined up, LFE too
        assert h.param("gain_db", GAIN_6DB) == "6.0000"
        y = h.process(x[:4096])
        assert np.allclose(y[1000:, 0], x[:3096, 0] * 10 ** (6 / 20), atol=1e-6)
        assert np.array_equal(y[1000:, 3], x[:3096, 3])                        # not used: as it was
    finally:
        h.close()
    assert h.proc.poll() is not None


@pytest.mark.parametrize("mode,groups", [("pairs", [[0, 1], [2, 3], [4, 5]]),
                                         ("each", [[0], [1], [2], [3], [4], [5]])])
def test_host_groups(plug, mode, groups):
    from sound.vst.client import Host
    h = Host()
    try:
        info = h.load(plug, PLAIN, RATE, 6, mode)
        assert info["mode"] == mode and info["groups"] == groups
        h.set(params={"gain_db": GAIN_6DB})                  # every instance
        x = _noise(5000, 6)
        assert np.allclose(h.process(x), x * 10 ** (6 / 20), atol=1e-6)
    finally:
        h.close()


def test_crash_and_hang_stay_in_the_host(plug, monkeypatch):
    from sound.vst import client
    h = client.Host()
    h.load(plug, PLAIN, RATE, 2)
    h.set(params={"crash": 1.0})
    with pytest.raises(client.PluginError) as e:
        h.process(_noise(1024, 2))
    assert e.value.kind == "crashed" and "упал" in str(e.value)
    with pytest.raises(client.PluginError):                    # dead stays dead, at once
        h.process(_noise(16, 2))
    h.close()
    monkeypatch.setattr(client, "T_PROCESS", 1.5)
    h = client.Host()
    h.load(plug, PLAIN, RATE, 2)
    h.set(params={"hang": 1.0})
    with pytest.raises(client.PluginError) as e:
        h.process(_noise(1024, 2))
    assert e.value.kind == "hung"
    assert h.proc.poll() is not None                            # killed
    h.close()


# ── the effect ────────────────────────────────────────────────
def test_render_gain_and_latency(plug, session):
    x = _noise(3 * RATE + 777, 6)
    d = _doc(session, x)
    eff = fx.get("vst3")
    v = {"plugin": _ref(plug, LATE), "params": {"gain_db": GAIN_6DB}}
    src = fx.render(d, 1000, d.frames - 500, eff, v)                 # the latency is made up for
    assert src.frames == d.frames - 1500
    y = src.read(0, src.frames)
    assert np.allclose(y, x[1000:-500] * 10 ** (6 / 20), atol=1e-6)
    assert eff.label(v) == LATE


def test_render_with_state_and_tail(plug, session):
    from sound.vst.client import Host
    h = Host()
    h.load(plug, PLAIN, RATE, 2)
    h.set(params={"gain_db": 18.0 / 48.0})                            # −6 dB
    state = h.get()["state"]
    h.close()
    from sound.fx.vst3 import b64
    x = _noise(RATE, 2)
    d = _doc(session, x)
    src = fx.render(d, 0, RATE // 2, fx.get("vst3"), {"plugin": _ref(plug), "state": b64(state)}, keep_tail=True)
    # no tail rings on: the render stops at the first quiet piece, its frames added to what follows
    assert RATE // 2 < src.frames <= RATE // 2 + store.CHUNK
    y = src.read(0, src.frames)
    g = 10 ** (-6 / 20)
    assert np.allclose(y[:RATE // 2], x[:RATE // 2] * g, atol=1e-6)
    assert np.allclose(y[RATE // 2:RATE], x[RATE // 2:], atol=1e-6) and not np.any(y[RATE:])


def test_render_crash_leaves_the_document(plug, session):
    from sound.vst.client import PluginError
    x = _noise(RATE, 2)
    d = _doc(session, x)
    with pytest.raises(PluginError):
        fx.render(d, 0, d.frames, fx.get("vst3"), {"plugin": _ref(plug), "params": {"crash": 1.0}})
    assert np.array_equal(d.read(0, d.frames), x)


def test_missing_plugin_found_by_id(plug, tmp_path, data_dir):
    from sound.fx import vst3
    from sound.vst import scan
    folder = tmp_path / "vst3"
    folder.mkdir()
    shutil.copytree(plug, folder / "ArtCoreTestPlug.vst3")
    cfg = config.load()
    cfg["vst3_folders"] = [str(folder)]
    config.save(cfg)
    scan._lib = None
    lib = scan.library(config.load())
    lib.scan()
    got = {p.name: p for p in lib.plugins()}
    ref = {"path": str(tmp_path / "gone" / "ArtCoreTestPlug.vst3"), "name": PLAIN, "id": got[PLAIN].id}
    assert vst3.resolve(ref)["path"] == str(folder / "ArtCoreTestPlug.vst3")
    with pytest.raises(FileNotFoundError):
        vst3.resolve({"path": str(tmp_path / "x.vst3"), "name": "Nothing", "id": "nope"})
    scan._lib = None


def test_clean_values():
    eff = fx.get("vst3")
    v = eff.clean({"plugin": {"path": "a.vst3", "name": "A", "junk": 1}, "state": 5, "params": {"g": 3, "h": "x"},
                   "mode": "nope", "use": [5, 1, 1, "x", -2]})
    assert v == {"plugin": {"path": "a.vst3", "name": "A", "id": "", "maker": ""}, "state": "",
                 "params": {"g": 1.0}, "mode": "auto", "use": [1, 5]}
    assert eff.clean(None)["plugin"] == {"path": "", "name": "", "id": "", "maker": ""}


# ── scanning ──────────────────────────────────────────────────
def test_scan_cache_and_failures(plug, tmp_path, data_dir, monkeypatch):
    from sound.vst import scan
    folder = tmp_path / "plugins"
    (folder / "sub").mkdir(parents=True)
    shutil.copytree(plug, folder / "sub" / "ArtCoreTestPlug.vst3")
    (folder / "Broken.vst3").write_bytes(b"not a plugin")
    cfg = config.load()
    cfg["vst3_folders"] = [str(folder)]
    lib = scan.Library(cfg)
    assert sorted(os.path.basename(f) for f in scan.find_files([str(folder)])) == ["ArtCoreTestPlug.vst3", "Broken.vst3"]
    n, bad = lib.scan()
    assert n == 2 and bad == 1
    names = sorted(p.name for p in lib.plugins())
    assert names == [PLAIN, LATE]
    p = next(p for p in lib.plugins() if p.name == PLAIN)
    assert p.maker == "ArtCore" and p.channels == [1, 2, 6, 8] and p.params == 3 and p.id
    assert lib.find({"id": p.id}) is p or lib.find({"id": p.id}).name == PLAIN
    # cached: nothing new, nothing opened
    opened = []
    real = scan.scan_one
    monkeypatch.setattr(scan, "scan_one", lambda f, timeout=scan.T_SCAN: opened.append(f) or real(f, timeout))
    assert scan.Library(cfg).scan() == (2, 1) and opened == []
    # «Retry» opens just those; a plugin that crashes while loaded: «crashed», the program goes on
    monkeypatch.setenv("ARTCORE_TESTPLUG_CRASH", "load")
    lib2 = scan.Library(cfg)
    lib2.scan(retry=[str(folder / "sub" / "ArtCoreTestPlug.vst3")])
    assert len(opened) == 1
    reasons = {os.path.basename(f.path): f.reason for f in lib2.failed()}
    assert reasons["ArtCoreTestPlug.vst3"] == "crashed" and lib2.plugins() == []
    monkeypatch.delenv("ARTCORE_TESTPLUG_CRASH")
    lib2.scan(force=True)
    assert len(lib2.plugins()) == 2


def test_scan_hang_times_out(plug, tmp_path, monkeypatch):
    from sound.vst import scan
    monkeypatch.setattr(scan, "T_SCAN", 0.05)
    assert scan.scan_one(plug, timeout=0.05) == {"failed": "hung"}


# ── the batch ─────────────────────────────────────────────────
def test_batch_step(plug, session):
    s = batch.Step("vst3", {"plugin": _ref(plug), "params": {"gain_db": GAIN_6DB}, "mode": "pairs"})
    assert s.title() == "VST3: " + PLAIN and s.values["keep_tail"] is True
    assert batch.Step.from_dict(s.to_dict()).values == s.values
    x = _noise(RATE, 6)
    d = _doc(session, x)
    batch.apply_step(d, batch.Step("vst3", {**s.values, "keep_tail": False}), lambda f: None)
    assert d.frames == RATE
    assert np.allclose(d.read(0, d.frames), x * 10 ** (6 / 20), atol=1e-6)


# ── the window ────────────────────────────────────────────────
def _window(qapp, session, data_dir, tmp_path, plug, ch=2):
    from conftest import ff
    from test_fx_ui import _open, _win
    p = str(tmp_path / f"tone{ch}.wav")
    layout = {2: "stereo", 6: "5.1"}[ch]
    ff("-f", "lavfi", "-i", "sine=f=440:d=2:r=48000", "-af", f"aformat=channel_layouts={layout},volume=0.25",
       "-c:a", "pcm_f32le", p)
    folder = tmp_path / "vst3"
    folder.mkdir(exist_ok=True)
    if not (folder / "ArtCoreTestPlug.vst3").exists():
        shutil.copytree(plug, folder / "ArtCoreTestPlug.vst3")
    from sound.vst import scan
    scan._lib = None
    w = _win(qapp, session, vst3_folders=[str(folder)])
    scan.library(w.cfg).scan()
    _open(qapp, w, p)
    return w


def _plugin(w, name=PLAIN):
    from sound.vst import scan
    return next(p for p in scan.library(w.cfg).plugins() if p.name == name)


def test_menu_lists_the_plugins(qapp, session, data_dir, tmp_path, plug):
    from test_fx_ui import _close
    w = _window(qapp, session, data_dir, tmp_path, plug)
    w._fill_vst_menu()
    top = {a.text(): a for a in w.vst_menu.actions()}
    assert "ArtCore" in top and "Сканировать плагины" in top
    inside = [a.text() for a in top["ArtCore"].menu().actions()]
    assert inside == [PLAIN, LATE]
    from sound import vstui
    vstui.remember(w.cfg, _plugin(w, LATE))
    w._fill_vst_menu()
    assert w.vst_menu.actions()[0].text() == LATE                        # the last used first
    _close(w)


def test_effect_window(qapp, session, data_dir, tmp_path, plug):
    from sound import fxui
    from sound.fx.vst3 import unb64
    from test_fx_ui import _close
    w = _window(qapp, session, data_dir, tmp_path, plug)
    d = w.doc
    x = d.read(0, d.frames).astype(np.float64)
    p = _plugin(w)
    dlg = fxui.EffectDialog(w, "vst3", d, 0, d.frames, True, plugin=p.ref())
    dlg.show()
    body = dlg.vst
    body.wait_loaded()
    assert body.host is not None and not body.error
    assert dlg.windowTitle() == PLAIN and set(body.ctrls) == {"gain_db", "crash", "hang"}
    assert dlg.values["state"]                                       # the plugin's state from the start
    slider, label = body.ctrls["gain_db"]
    slider.setValue(round(GAIN_6DB * slider.maximum()))
    assert label.text() == "6.0000 dB" and dlg.values["params"] == {"gain_db": pytest.approx(GAIN_6DB, abs=1e-3)}
    assert body.read_state() and dlg.values["params"] == {}
    # a preset keeps the state; another value; the preset back
    dlg.save_preset("Plus six")
    assert "vst3:" + p.id in w.cfg["fx_presets"]
    slider.setValue(round(0.5 * slider.maximum()))
    body.read_state()
    i = dlg.presets.findData("user:Plus six")
    dlg.presets.setCurrentIndex(i)
    dlg._preset_chosen(i)
    assert body.ctrls["gain_db"][1].text() == "6.0000 dB"
    # .vstpreset out and in
    vp = str(tmp_path / "plus6.vstpreset")
    assert body.export_preset(vp) and os.path.getsize(vp) > 0
    slider.setValue(0)
    body.read_state()
    assert body.ctrls["gain_db"][1].text() == "-24.0000 dB"
    assert body.import_preset(vp) and body.ctrls["gain_db"][1].text() == "6.0000 dB"
    # its own window: none here (no display / no editor) — said, not fatal
    body.b_editor.setChecked(True)
    assert pump(qapp, 5, lambda: not body.b_editor.isChecked() or body.read_state() is None)
    pump(qapp, 0.5)
    body.read_state()
    assert not body.b_editor.isChecked() and "Окно плагина не открылось" in dlg.status.text()
    # the preview runs through a host of its own
    dlg.b_preview.setChecked(True)
    assert pump(qapp, 10, lambda: dlg.chain is not None and dlg.chain.blocks > 3)
    assert not dlg.chain.error and "все сразу" in body.taken.text()
    dlg.b_preview.setChecked(False)
    values = dlg.result_values()
    state = unb64(values["state"])
    dlg.accept()
    assert state and body.host is None                                   # its host closed with the window
    w.apply_effect("vst3", values)
    assert pump(qapp, 30, lambda: w.job is None and len(d.history) == 2)
    assert np.allclose(d.read(0, d.frames), x * 10 ** (6 / 20), atol=1e-5)
    assert "vst3:" + p.id in w.cfg["fx_last"]
    _close(w)


def test_effect_window_plugin_crash(qapp, session, data_dir, tmp_path, plug, monkeypatch):
    """The plugin dies during the preview: the window says so, the program and the document go on."""
    from PySide6.QtWidgets import QMessageBox
    from sound import fxui
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    from test_fx_ui import _close
    w = _window(qapp, session, data_dir, tmp_path, plug)
    d = w.doc
    x = d.read(0, d.frames)
    dlg = fxui.EffectDialog(w, "vst3", d, 0, d.frames, True, plugin=_plugin(w).ref())
    dlg.show()
    dlg.vst.wait_loaded()
    dlg.b_preview.setChecked(True)
    assert pump(qapp, 10, lambda: dlg.chain is not None and dlg.chain.blocks > 2)
    dlg.vst.ctrls["crash"][0].setChecked(True)
    assert pump(qapp, 10, lambda: "упал" in dlg.status.text())
    dlg.reject()
    w.apply_effect("vst3", {"plugin": _plugin(w).ref(), "params": {"crash": 1.0}})
    assert pump(qapp, 30, lambda: w.job is None)
    assert len(d.history) == 1 and np.array_equal(d.read(0, d.frames), x)
    assert len(warned) == 1 and "упал" in warned[0]
    _close(w)


def test_settings_page(qapp, data_dir, tmp_path, plug):
    from sound.dialogs import SettingsDialog
    from sound.vst import scan
    folder = tmp_path / "vst3"
    folder.mkdir()
    shutil.copytree(plug, folder / "ArtCoreTestPlug.vst3")
    (folder / "Broken.vst3").write_bytes(b"x")
    cfg = config.load()
    scan._lib = None
    dlg = SettingsDialog(cfg, None, page="vst3")
    assert dlg.tabs.currentWidget() is dlg.vst
    pg = dlg.vst
    assert pg.folder_list() == scan.default_folders()
    pg.folders.clear()
    pg.folders.addItem(str(folder))
    pg.start_scan(False)
    pg.wait()
    assert pg.list.topLevelItemCount() == 2 and pg.failed.count() == 1
    assert pg.list.topLevelItem(0).text(3) == "1.0, 2.0, 5.1, 7.1"
    assert "Найдено плагинов: 2, не загрузились: 1" == pg.state.text()
    dlg.apply()
    assert cfg["vst3_folders"] == [str(folder)]
    pg._defaults()
    pg.apply()
    assert cfg["vst3_folders"] == []
    scan._lib = None


def test_macro_with_a_plugin(plug, session):
    """A VST3 step in a macro, over the selection the macro made."""
    from sound import macro
    x = _noise(2 * RATE, 2)
    d = _doc(session, x)
    macro.run(d, [macro.Step("select", {"a": 1.0, "a_from": "end", "b": 0.0, "b_from": "end"}),
                  macro.Step("vst3", {"plugin": _ref(plug, LATE), "params": {"gain_db": GAIN_6DB}})])
    y = d.read(0, d.frames)
    assert d.frames == 2 * RATE
    assert np.array_equal(y[:RATE], x[:RATE])
    assert np.allclose(y[RATE:], x[RATE:] * 10 ** (6 / 20), atol=1e-6)
