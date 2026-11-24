"""3.0 — the multitrack tab in the window: a new project, files dropped on the timeline, clips dragged with the mouse
(one undo step), the headers and the mixer changing the project, playing with the meters, recording onto an armed
track, «Send to multitrack» and «Open clip in editor» and back, the mixdown, saving / opening, crash recovery."""
import os

import numpy as np
from conftest import pump
from PySide6.QtCore import QEvent, QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from sound import config
from sound.waveview import RULER_H


def _win(qapp, session, **over):
    from sound.window import MainWindow
    cfg = config.load()
    cfg.update(check_updates=False, geometry="")
    cfg.update(over)
    w = MainWindow(cfg, session)
    w.resize(1500, 900)
    w.show()
    pump(qapp, 0.1)
    return w


def _close(w):
    for d in w.docs:
        d.mark_saved()
    for p in w.projects:
        p.mark_saved()
    w.close()


def _mouse(widget, kind, x, y, button=Qt.LeftButton, mods=Qt.NoModifier):
    ev = QMouseEvent(kind, QPointF(x, y), widget.mapToGlobal(QPointF(x, y)), button,
                     button if kind != QEvent.MouseButtonRelease else Qt.NoButton, mods)
    QApplication.sendEvent(widget, ev)


def _new(w, tmp_path, tracks=2, channels=2):
    from sound.mt import projfile
    from sound.mt.model import Project
    folder = tmp_path / "P"
    folder.mkdir(exist_ok=True)
    p = Project(48000, channels, "P", str(folder / "P.acproj"), str(folder))
    for i in range(tracks):
        p.add_track(f"T{i + 1}")
    projfile.save(p)
    w.add_project(p)
    return p


def _insert(qapp, w, p, paths, tid=None, frame=0):
    n = sum(len(x.clips) for x in p.tracks)
    w.mt_insert(paths, tid, frame)
    assert pump(qapp, 30, lambda: w.job is None and sum(len(x.clips) for x in p.tracks) >= n + len(paths))


def test_project_tab_edit_and_undo(qapp, session, data_dir, media_dir, tmp_path):
    w = _win(qapp, session)
    p = _new(w, tmp_path)
    assert w.proj is p and w.doc is None and w.center_stack.currentIndex() == 1
    assert w.a_mt_add.isEnabled() and not w.a_mt_send.isEnabled()
    _insert(qapp, w, p, [os.path.join(media_dir, "stereo16.wav"), os.path.join(media_dir, "song.mp3")],
            p.tracks[0].id)
    assert [len(x.clips) for x in p.tracks] == [1, 1]
    tl = w.mt_view.timeline
    tl.zoom_all()
    pump(qapp, 0.1)
    # drag the first clip to the right with the mouse: one undo step
    c = p.tracks[0].clips[0]
    lane_y = RULER_H + 50
    x0 = tl.x_of(c.start) + 40
    steps = len(p.history)
    _mouse(tl, QEvent.MouseButtonPress, x0, lane_y)
    for dx in (20, 60, 120):
        _mouse(tl, QEvent.MouseMove, x0 + dx, lane_y, Qt.NoButton, Qt.ShiftModifier)
    _mouse(tl, QEvent.MouseButtonRelease, x0 + 120, lane_y)
    moved = p.tracks[0].clips[0]
    assert moved.start > c.start and len(p.history) == steps + 1
    w.undo()
    assert p.tracks[0].clips[0].start == c.start
    w.redo()
    # the header's fader and the mixer's mute change the project
    tid = p.tracks[1].id
    w.mt_view.headers.headers[tid].vol.setValue(-120)
    assert abs(p.track(tid).gain_db + 12.0) < 1e-9
    w.mt_view.mixer.strips[tid].b_mute.click()
    assert p.track(tid).mute
    # split at the cursor, copy / paste, delete
    p.cursor = moved.start + 10000
    p.sel_clips = frozenset()
    w.mt_split()
    assert len(p.tracks[0].clips) == 2
    p.sel_clips = frozenset({p.tracks[0].clips[1].id})
    w.copy()
    p.cursor = 200000
    p.ui["current"] = p.tracks[0].id
    w.paste()
    assert len(p.tracks[0].clips) == 3
    w.delete()
    assert len(p.tracks[0].clips) == 2
    # markers work on the project
    w.add_marker()
    assert len(p.markers) == 1 and w.mlist.topLevelItemCount() == 1
    _close(w)


def test_drop_play_meters_and_mixdown(qapp, session, data_dir, media_dir, tmp_path):
    from sound.mt.model import MASTER
    w = _win(qapp, session)
    p = _new(w, tmp_path, tracks=0)
    tl = w.mt_view.timeline
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(os.path.join(media_dir, "stereo16.wav"))])
    ev = QDropEvent(QPointF(200, RULER_H + 30), Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier)
    tl.dropEvent(ev)                                   # onto no track: a track of its own (Qt drops only in a drag)
    assert pump(qapp, 30, lambda: w.job is None and p.tracks and p.tracks[0].clips)
    p.cursor = p.tracks[0].clips[0].start                # dropped where the mouse was
    w.play()
    assert pump(qapp, 3, lambda: w.player.state == "play" and w.player.position() > p.cursor + 20000)
    pump(qapp, 0.3)
    assert w.meters.level.max() > -60                 # the master's meters move
    assert any(h.meter.level.max() > -60 for h in w.mt_view.headers.headers.values())
    w.stop_play()
    assert w.player.state == "stop"
    out = str(tmp_path / "mix.flac")
    from sound.mt import render
    render.mixdown(p, out, "flac", "24")
    assert os.path.getsize(out) > 1000
    assert MASTER in w.mt_engine(p).nodes
    _close(w)


def test_record_onto_an_armed_track(qapp, session, data_dir, tmp_path):
    w = _win(qapp, session)
    p = _new(w, tmp_path, tracks=1)
    tid = p.tracks[0].id
    w.mt_view.headers.headers[tid].b_arm.click()       # armed: the input opens (meters)
    assert w.mt_rec is not None and p.ui["armed"] == [tid]
    p.cursor = 24000
    w.record_toggle()
    assert w.mt_recording is not None and w.player.state == "play"
    pump(qapp, 1.0)
    assert tid in w.mt_view.timeline.rec_preview
    w.record_toggle()
    assert w.mt_recording is None
    c = p.tracks[0].clips[0]
    assert abs(c.start - 24000) < 3 * 1024 and c.length > 24000
    m = p.media[c.media]
    assert os.path.dirname(m.path).endswith("P Audio") and m.path.endswith(".wav")
    w.undo()
    assert not p.tracks[0].clips
    w.mt_view.headers.headers[tid].b_arm.click()
    assert w.mt_rec is None
    _close(w)


def test_send_to_multitrack_and_edit_a_clip(qapp, session, data_dir, media_dir, tmp_path):
    w = _win(qapp, session)
    w.open_paths([os.path.join(media_dir, "mono24.wav")])
    assert pump(qapp, 30, lambda: w.doc is not None and w.job is None)
    d = w.doc
    p = _new(w, tmp_path, tracks=1)
    w.tabs.setCurrentIndex(w.items.index(d))
    d.sel = (0, 48000)
    w.mt_send_to()
    assert pump(qapp, 30, lambda: w.job is None and p.tracks[0].clips)
    assert w.proj is p
    c = p.tracks[0].clips[0]
    assert c.length == 48000
    x = p.media[c.media].src.read(0, 48000)
    assert np.array_equal(x, d.read(0, 48000))
    # open the clip in the editor, change it, save: the clip plays the edited copy
    p.sel_clips = frozenset({c.id})
    w.mt_open_clip(None)
    assert pump(qapp, 30, lambda: w.job is None and isinstance(w.items[w.tabs.currentIndex()], type(d)))
    e = w.doc
    assert e.mt_link["project"] is p and "P Audio" in e.path
    e.silence(0, 1000)
    assert w.save(wait=True)
    c2 = p.tracks[0].clips[0]
    assert c2.media != c.media and not p.media[c2.media].src.read(0, 1000).any()
    assert os.path.isfile(p.media[c.media].path)        # the original is still there
    w.mt_back_to_project()
    assert w.proj is p
    w.undo()
    assert p.tracks[0].clips[0].media == c.media
    _close(w)


def test_save_open_and_recover(qapp, session, data_dir, media_dir, tmp_path):
    from sound import recovery
    from sound.mt import projfile
    w = _win(qapp, session)
    p = _new(w, tmp_path, tracks=1)
    _insert(qapp, w, p, [os.path.join(media_dir, "stereo16.wav")], p.tracks[0].id)
    assert w.save()
    assert not p.dirty
    path = p.path
    p.set_track(p.tracks[0].id, gain_db=-5)
    w.mt_edited(p)
    w.write_journals(True)
    jp = os.path.join(p.journal_folder, projfile.JOURNAL)
    assert os.path.isfile(jp)
    # as if crashed: another run finds the journal
    from sound import store
    sess_root = os.path.dirname(session.path)
    data = projfile.journal_dict(p)
    lost = recovery.Entry(session.path, p.journal_folder, data, "project")
    for x in w.projects:
        x.mark_saved()
    w.close_all()
    assert not w.items
    w2 = _win(qapp, store.Session(sess_root, clean=False))
    q = w2.mt_restore(lost)
    assert q.track(q.tracks[0].id).gain_db == -5 and q.dirty and len(q.history) == len(data["history"])
    _close(w2)
    # opening the saved file
    w3 = _win(qapp, session)
    w3.open_paths([path])
    assert pump(qapp, 30, lambda: w3.proj is not None and w3.job is None)
    assert w3.proj.tracks[0].clips and not w3.proj.dirty
    _close(w3)


def test_english_menu(qapp, session, data_dir):
    from sound import i18n
    i18n.set_lang("en")
    try:
        w = _win(qapp, session)
        assert "Multitrack" in [a.text() for a in w.menuBar().actions()]
        assert w.a_mt_new.text().startswith("New multitrack project")
        _close(w)
    finally:
        i18n.set_lang("ru")
