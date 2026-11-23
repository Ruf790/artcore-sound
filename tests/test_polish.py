"""Polish (1.0): the shortcuts (unique, the tool-bar tips made from them, the Hotkeys window, the README's table),
the strings (every key there, RU and EN alike), the window remembering itself, the installer's «Open with», the
update from 0.4, the working files of an undone branch freed, long paths with Cyrillic names, both themes."""
import ast
import glob
import json
import os
import re
import shutil

import numpy as np
from conftest import pump

from sound import AUDIO_EXT, VERSION, VERSION_TUPLE, VIDEO_EXT, batch, config, media, proc, store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _win(qapp, session, **over):
    from sound.window import MainWindow
    cfg = config.load()
    cfg.update(check_updates=False, geometry="")
    cfg.update(over)
    w = MainWindow(cfg, session)
    w.resize(1400, 860)
    w.show()
    pump(qapp, 0.05)
    return w


def test_shortcuts_unique_and_tips_follow_them(qapp, session, data_dir):
    from PySide6.QtGui import QAction
    from sound import keys
    w = _win(qapp, session)
    seen = {}
    for a in w.findChildren(QAction):
        for k in a.shortcuts():
            s = k.toString()
            assert s not in seen, f"{s}: «{seen[s]}» and «{a.text()}»"
            seen[s] = a.text()
    assert len(seen) > 45
    # every tip of the tool bar and the transport that names a key names the key of its action
    acts = {keys.plain(a.text()): keys.action_keys(a) for a in w.findChildren(QAction) if a.shortcuts()}
    from PySide6.QtWidgets import QToolButton
    tips = [b.toolTip() for b in w.findChildren(QToolButton) if b.toolTip()]
    keyed = [x for x in tips if re.search(r"\(([^()]+)\)$", x)]
    assert len(keyed) >= 20
    all_keys = {k for v in acts.values() for k in v.split(" / ")}
    for tip in keyed:
        k = re.search(r"\(([^()]+)\)$", tip).group(1)
        assert k in all_keys, tip
    assert w.tb["undo"].toolTip().endswith("(Ctrl+Z)") and w.t_play.toolTip().endswith("(Пробел)")
    assert keys.key_text("Ctrl+Shift+Up") == "Ctrl+Shift+↑" and keys.key_text("Ctrl++") == "Ctrl++"
    # the Hotkeys window has them all, and its filter finds one
    from sound.dialogs import KeysDialog
    dlg = KeysDialog(w.menuBar(), w)
    rows = keys.table(w.menuBar())
    assert len(rows) == len([a for a in w.findChildren(QAction) if a.shortcuts() and a.parent() is w])
    texts = [dlg.tree.topLevelItem(i).text(1) for i in range(dlg.tree.topLevelItemCount())]
    assert "Ctrl+B" in texts and "F1" in texts and "Shift+щелчок" in texts
    dlg.filter.setText("пакет")
    shown = [dlg.tree.topLevelItem(i).text(0).strip() for i in range(dlg.tree.topLevelItemCount())
             if not dlg.tree.topLevelItem(i).isHidden()]
    assert shown == ["Пакетная обработка"]
    # the README's table lists every shortcut (tools/hotkeys_md.py writes it)
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    for _menu, cmd, k in rows:
        assert f"| {k.replace('|', '\\|')} |" in readme or f"`{k}`" in readme, (cmd, k)
    for d in w.docs:
        d.mark_saved()
    w.close()


def test_strings_complete():
    from sound import i18n
    used = set()
    dynamic = set()
    for f in glob.glob(os.path.join(ROOT, "sound", "**", "*.py"), recursive=True):
        src = open(f, encoding="utf-8").read()
        used |= set(re.findall(r'\bt\(\s*"([a-z0-9_]+)"', src))
        dynamic |= set(re.findall(r'\bt\(\s*f?"([a-z0-9_]+_)"\s*\+', src))
        dynamic |= set(re.findall(r'\bt\(\s*f"([a-z0-9_]+)\{', src))
        tree = ast.parse(src)
        for node in ast.walk(tree):                     # the labels of fx.Param / the batch's params
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Param":
                for kw in node.keywords:
                    if kw.arg == "label" and isinstance(kw.value, ast.Constant):
                        used.add(kw.value.value)
    missing = sorted(k for k in used if k and not k.endswith("_") and k not in i18n.STRINGS)
    assert missing == []
    for pre in dynamic:
        assert any(k.startswith(pre) for k in i18n.STRINGS), pre
    cyr = re.compile("[А-Яа-яЁё]")
    for k, (ru, en) in i18n.STRINGS.items():
        assert ru.strip() and en.strip(), k
        assert not cyr.search(en), (k, en)
        assert sorted(re.findall(r"\{\d\}", ru)) == sorted(re.findall(r"\{\d\}", en)), k


def test_version_everywhere():
    iss = open(os.path.join(ROOT, "setup.iss"), encoding="utf-8-sig").read()
    vi = open(os.path.join(ROOT, "version_info.txt"), encoding="utf-8").read()
    assert f'#define AppVersion   "{VERSION}"' in iss
    assert ".".join(map(str, VERSION_TUPLE[:3])) == VERSION
    assert f"filevers=({', '.join(map(str, VERSION_TUPLE))})" in vi and f"'{VERSION}" in vi


def test_installer_open_with_every_format():
    iss = open(os.path.join(ROOT, "setup.iss"), encoding="utf-8-sig").read()
    types = set(re.findall(r'SupportedTypes"; ValueType: string; ValueName: "(\.[a-z0-9]+)"', iss))
    lists = set(re.findall(r'Classes\\(\.[a-z0-9]+)\\OpenWithList', iss))
    want = set(AUDIO_EXT + VIDEO_EXT)
    assert types == want and lists == want


def test_update_from_04(monkeypatch):
    from sound import updater
    assert updater._ver("1.0.0") > updater._ver("0.4.0") > updater._ver("0.3.9")
    rel = {"tag_name": "1.0.0", "body": "notes", "html_url": "https://x/1.0.0", "assets": [
        {"name": "ArtCoreSound-portable.zip", "browser_download_url": "https://x/p.zip", "size": 1},
        {"name": "ArtCoreSound-Setup-1.0.0.exe", "browser_download_url": "https://x/s.exe", "size": 123}]}

    class R:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            import io
            return io.BytesIO(json.dumps(self.data).encode())

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(updater, "VERSION", "0.4.0")
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: R(rel))
    got = updater.check()
    assert got["version"] == "1.0.0" and got["url"] == "https://x/s.exe" and got["size"] == 123
    monkeypatch.setattr(updater, "VERSION", "1.0.0")
    assert updater.check() is None
    assert "/SILENT" in updater.installer_cmdline("C:\\T\\ArtCoreSound-Setup-1.0.0.exe")


def test_window_remembers(qapp, session, data_dir, media_dir):
    from sound import fxui, winstate
    w = _win(qapp, session)
    w.splitter.setSizes([900, 400])
    pump(qapp, 0.05)
    sizes = w.splitter.sizes()
    w.close()
    cfg = config.load()
    assert cfg["main_split"] == sizes
    w2 = _win(qapp, session)
    assert w2.splitter.sizes() == sizes
    # a dialog's size by its class (the event filter of main.py does this for every window)
    winstate.cfg = w2.cfg
    from sound.doc import new_document
    d = new_document(session.folder(), 48000, 2)
    dlg = fxui.EffectDialog(w2, "delay", d, 0, 0, True)
    dlg.show()
    dlg.resize(760, 520)
    pump(qapp, 0.05)
    winstate.save(dlg)
    dlg.reject()
    dlg2 = fxui.EffectDialog(w2, "delay", d, 0, 0, True)
    assert winstate.restore(dlg2)
    dlg2.show()
    pump(qapp, 0.05)
    assert (dlg2.size().width(), dlg2.size().height()) == (760, 520)
    dlg2.reject()
    winstate.cfg = None
    w2.close()


def test_undone_branch_frees_its_files(qapp, session, data_dir, media_dir):
    w = _win(qapp, session)
    w.open_paths([os.path.join(media_dir, "stereo16.wav")])
    assert pump(qapp, 30, lambda: w.docs and w.job is None)
    d = w.doc

    def f32():
        return sorted(n for n in os.listdir(d.folder) if n.endswith(".f32"))
    base = f32()
    d.replace(0, d.frames, proc.apply_op(d, "gain", 0, d.frames, db=-3.0), "gain")
    w._edited()
    first = f32()
    assert len(first) == len(base) + 1
    d.undo()
    d.replace(0, d.frames, proc.apply_op(d, "invert", 0, d.frames), "invert")
    w._edited()
    assert len(f32()) == len(base) + 1 and set(f32()) != set(first)      # the undone gain's file is gone
    # a source the clipboard still holds stays
    d.sel = (0, d.frames)
    w.copy()
    d.undo()
    d.replace(0, d.frames, proc.apply_op(d, "gain", 0, d.frames, db=-1.0), "gain2")
    w._edited()
    assert len(f32()) == len(base) + 2
    w.clip = None
    d.undo()
    d.silence(0, 10)                                 # both branches dropped, the clipboard holds nothing: all freed
    w._edited()
    assert f32() == base
    for x in w.docs:
        x.mark_saved()
    w.close()


def test_long_cyrillic_paths(media_dir, tmp_path):
    """Files deep in folders with Cyrillic names (a path of more than 260 characters) open, save and go through a
    batch."""
    deep = str(tmp_path)
    while len(deep) < 300:
        deep = os.path.join(deep, "Очень длинная папка с музыкой — сборник «Лучшее»")
    os.makedirs(deep)
    src = os.path.join(deep, "Песня №1 (ремастер).flac")
    shutil.copy(os.path.join(media_dir, "hires.flac"), src)
    assert len(src) > 260
    s = store.Session(os.path.join(str(tmp_path), "w"))
    d = media.open_file(src, s.folder())
    out = os.path.join(deep, "Песня №1 — копия.wav")
    media.save(d, out, "wav", "24")
    assert os.path.isfile(out)
    job = batch.Job(batch.Source("folder", str(tmp_path), True, "flac"), [batch.Step("gain", {"db": -2.0})],
                    batch.Output("mp3", "192", "same", "", True, "{name}", " (тише)"))
    r = batch.Runner(job, workers=1)
    r.start()
    assert r.wait(60)
    assert r.tasks[0].status == "ok", r.tasks[0].error
    assert os.path.isfile(os.path.join(deep, "Песня №1 (ремастер) (тише).mp3"))
    s.close()


def test_windows_in_both_themes(qapp, session, data_dir, media_dir):
    from sound import crash, recovery, style
    from sound.batchui import BatchWindow, StepDialog
    from sound.dialogs import KeysDialog, RecoveryDialog
    for theme in ("light", "dark"):
        style.set_theme(theme)
        qapp.setStyleSheet(style.stylesheet())
        w = _win(qapp, session, theme=theme)
        for dlg in (BatchWindow(w), KeysDialog(w.menuBar(), w), StepDialog(batch.Step("normalize"), w),
                    RecoveryDialog([recovery.Entry("s", "f", {"name": "Песня.wav", "rate": 48000, "frames": 96000,
                                                              "time": 1.7e9, "pos": 3})], w),
                    crash.CrashDialog("Traceback …", "C:\\crash.log", w)):
            dlg.show()
            pump(qapp, 0.02)
            img = dlg.grab().toImage()
            c = img.pixelColor(img.width() // 2, 3)
            assert (c.lightness() > 128) == (theme == "light"), (type(dlg).__name__, theme, c.name())
            dlg.close()
        w.close()
    style.set_theme("dark")
    qapp.setStyleSheet(style.stylesheet())
    assert np.isfinite(1.0)


def test_low_disk_forgets_redo_before_a_long_job(qapp, session, data_dir, media_dir, monkeypatch):
    from sound import winint
    w = _win(qapp, session)
    w.open_paths([os.path.join(media_dir, "stereo16.wav")])
    assert pump(qapp, 30, lambda: w.docs and w.job is None)
    d = w.doc
    w.simple_op("invert")
    assert pump(qapp, 20, lambda: w.job is None and d.can_undo())
    w.undo()
    files = sorted(n for n in os.listdir(d.folder) if n.endswith(".f32"))
    assert d.can_redo() and len(files) == 2
    toasts = []
    monkeypatch.setattr(w, "_toast", lambda text: toasts.append(text))
    monkeypatch.setattr(winint, "free_space", lambda _p: 1000)
    w.simple_op("reverse")
    assert pump(qapp, 20, lambda: w.job is None and d.can_undo())
    assert any("Повторить" in x for x in toasts)
    left = sorted(n for n in os.listdir(d.folder) if n.endswith(".f32"))
    assert len(left) == 2 and files[1] not in left or files[0] not in left
    monkeypatch.undo()
    for x in w.docs:
        x.mark_saved()
    w.close()
