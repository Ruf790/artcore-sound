"""Screenshots of the window (offscreen Qt) in the dark and the light theme: the empty window, a stereo file with a
selection while it plays (the status bar measured), the sample-level zoom, a 5.1 file, Tools → Statistics of the
5.1 file, the Normalize dialog, and (0.3) the effect windows while they preview: the parametric EQ, the compressor,
the noise reduction with its noise sample, the reverb; (0.4) markers and regions with their list, the spectrum
panel while playing, the spectrogram and «wave + spectrogram» views, Tools → Spectrum, the spectrogram settings and
the Record window recording from the null input; (1.0) the batch window after a run, the Hotkeys window, the
recovery offer, the error window and the long-film report (ARTCORE_STRESS_REPORT); (2.0) the listening volume, the
film tools, and the restoration — its menu, its effect windows (the hum found, the clipping found and marked), the
spectral editing window beside a selection drawn on the spectrogram. Test sound is made by ffmpeg.

Usage: python tools/screenshots.py OUT_DIR [ru|en] [--quick | --restore]   (--quick: the main window and 1.0's
windows only — QT_SCALE_FACTOR=1.5 with it is the 150 % check; --restore: the main window and the restoration only)"""
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ARTCORE_AUDIO", "null")
TMP = tempfile.mkdtemp(prefix="artcore_sound_shots_")
RESTORE = "--restore" in sys.argv
os.environ.setdefault("ARTCORE_SOUND_DATA", os.path.join(TMP, "data"))

SONG = ("aevalsrc='0.55*sin(2*PI*110*t)*exp(-mod(t,0.5)*5)+0.25*sin(2*PI*440*t+3*sin(2*PI*2*t))*(0.6+0.4*sin(2*PI*0.15*t))"
        "+0.08*(random(0)-0.5)|0.5*sin(2*PI*110*t+0.3)*exp(-mod(t+0.25,0.5)*5)+0.25*sin(2*PI*554*t)"
        "*(0.6+0.4*cos(2*PI*0.1*t))+0.08*(random(1)-0.5)':s=44100:d=95")


def ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", *args], check=True)


def main(out: str, lang: str = "ru") -> int:
    os.makedirs(out, exist_ok=True)
    song = os.path.join(TMP, "song.flac")
    surround = os.path.join(TMP, "film.ac3")
    ff("-f", "lavfi", "-i", SONG, "-c:a", "flac", "-sample_fmt", "s16", song)
    ff("-f", "lavfi", "-i", "aevalsrc='0.6*sin(2*PI*220*t)*sin(PI*t/6)|0.6*sin(2*PI*330*t)*sin(PI*t/6)"
       "|0.5*sin(2*PI*180*t)*(0.5+0.5*sin(2*PI*0.5*t))|0.7*sin(2*PI*45*t)*exp(-mod(t,1)*3)"
       "|0.3*(random(0)-0.5)*(1+sin(2*PI*0.2*t))|0.3*(random(1)-0.5)*(1-sin(2*PI*0.2*t))':s=48000:d=12:c=5.1(side)",
       "-c:a", "ac3", "-b:a", "448k", surround)
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    from sound import config, media, store, style
    from sound.i18n import set_lang
    set_lang(lang)

    def pump(sec):
        t0 = time.time()
        while time.time() - t0 < sec:
            app.processEvents()
            time.sleep(0.01)

    for theme in ("dark", "light"):
        style.set_theme(theme)
        app.setStyle("Fusion")
        app.setStyleSheet(style.stylesheet())
        from sound.window import MainWindow
        cfg = config.load()
        cfg.update(check_updates=False, theme=theme, geometry="")
        s = store.Session(os.path.join(TMP, "work"))
        w = MainWindow(cfg, s)
        w.resize(1500, 900)
        w.show()
        pump(0.3)
        w.grab().save(os.path.join(out, f"empty_{theme}.png"))
        w.add_doc(media.open_file(song, s.folder()))
        w.add_doc(media.open_file(surround, s.folder()))
        w.tabs.setCurrentIndex(0)
        pump(0.2)
        d = w.doc
        mk = ("Вступление", "Куплет", "Припев") if lang == "ru" else ("Intro", "Verse", "Chorus")
        d.add_marker(int(4.0 * 44100), mk[0])
        d.add_marker(int(16.0 * 44100), mk[1])
        d.add_region(int(36.0 * 44100), int(52.0 * 44100), mk[2])
        d.add_region(int(60.0 * 44100), int(71.5 * 44100), mk[2] + " 2")
        d.add_marker(int(80.0 * 44100), "Outro")
        d.mark_saved()
        w._markers_changed()
        d.sel = (int(20.5 * 44100), int(31.2 * 44100))
        d.cursor = d.sel[0]
        w.wave.refresh()
        w._on_selection()
        w.play()
        pump(1.2)
        t0 = time.time()
        while "…" in w.st_stats.text() and time.time() - t0 < 20:
            pump(0.1)
        w.grab().save(os.path.join(out, f"main_{theme}.png"))
        w.stop_play()
        if RESTORE:
            for x in w.docs:
                x.mark_saved()
            shoot_restore(w, out, theme, pump, lang)
            for x in w.docs:
                x.mark_saved()
            w.close()
            s.close()
            continue
        if QUICK:
            for x in w.docs:
                x.mark_saved()
            shoot_10(w, out, theme, pump, lang)
            w.close()
            s.close()
            continue
        w.wave.zoom_range(int(20.5 * 44100), int(20.53 * 44100))
        pump(0.2)
        w.grab().save(os.path.join(out, f"samples_{theme}.png"))
        w.tabs.setCurrentIndex(1)
        pump(0.2)
        w.doc.sel = (int(3 * 48000), int(7.5 * 48000))
        w.wave.refresh()
        w._on_selection()
        w.play()
        pump(0.8)
        w.grab().save(os.path.join(out, f"surround_{theme}.png"))
        w.stop_play()
        w.select_none()
        w.statistics_dialog()
        dlg = w.stats_dlg
        t0 = time.time()
        while dlg.prog is not None and time.time() - t0 < 30:
            pump(0.1)
        dlg.resize(720, 560)
        pump(0.2)
        dlg.grab().save(os.path.join(out, f"statistics_{theme}.png"))
        dlg.close()
        from sound.dialogs import NormalizeDialog
        nd = NormalizeDialog({"normalize": {"mode": "loudness", "target": -14.0, "ceiling": -1.0}},
                             "Сейчас пик −0.85 dBFS · −16.2 LUFS" if lang == "ru" else
                             "The peak now is −0.85 dBFS · −16.2 LUFS", False, w)
        nd.show()
        pump(0.2)
        nd.grab().save(os.path.join(out, f"normalize_{theme}.png"))
        nd.close()
        if not QUICK:
            shoot_effects(w, out, theme, pump)
            shoot_04(w, out, theme, pump, lang)
        shoot_10(w, out, theme, pump, lang)
        shoot_20(w, out, theme, pump, lang)
        for x in w.docs:
            x.mark_saved()
        w.close()
        s.close()
        pump(0.1)
    print("screenshots in", out)
    return 0


def shoot_effects(w, out: str, theme: str, pump) -> None:
    """The effect windows over a selection of the song, previewing (the meters and the reduction live)."""
    from sound import fx, fxui
    w.tabs.setCurrentIndex(0)
    pump(0.2)
    d = w.doc
    import numpy as np
    hiss = 0.08 * (np.random.default_rng(0).random((66150, 2)) - 0.5)     # the song's own noise, alone
    fx.get("nr").profile = {**fx.noise_profile([hiss], 44100, 2), "seconds": 1.5}
    d.sel = (int(20.5 * 44100), int(31.2 * 44100))
    w.wave.refresh()
    w._on_selection()
    shots = (("eq", {"b1_on": True, "b1_freq": 45.0, "b2_gain": 4.5, "b2_freq": 110.0, "b3_gain": -3.5,
                     "b3_freq": 320.0, "b3_q": 1.6, "b5_gain": 3.0, "b5_freq": 3200.0, "b6_gain": 2.5}, 980, 700),
             ("comp", {"threshold": -26.0, "ratio": 4.0, "knee": 8.0, "attack": 8.0, "release": 160.0,
                       "makeup": 6.0}, 960, 430),
             ("nr", {"reduction": 18.0, "sensitivity": 6.0}, 780, 620),
             ("reverb", {"size": 80.0, "decay": 2.4, "predelay": 25.0, "mix": 30.0}, 720, 470))
    for key, vals, wd, ht in shots:
        a, b = d.sel
        dlg = fxui.EffectDialog(w, key, d, a, b, False)
        dlg.set_values({**dlg.values, **vals})
        dlg.resize(wd, ht)
        dlg.show()
        dlg.b_preview.setChecked(True)
        pump(1.0)
        dlg.grab().save(os.path.join(out, f"fx_{key}_{theme}.png"))
        dlg.reject()
        pump(0.1)


def _tiles(w, pump) -> None:
    w.wave.repaint()
    t0 = time.time()
    while time.time() - t0 < 30:
        pump(0.1)
        if w.wave.engine is not None and w.wave.sg_pending() == 0 and w.wave._sg_ready:
            break
    pump(0.2)


def shoot_04(w, out: str, theme: str, pump, lang: str) -> None:
    """Markers, the spectrogram views, the spectrum window, the spectrogram settings, the Record window."""
    from sound.anaui import SpectrogramDialog
    w.tabs.setCurrentIndex(0)
    pump(0.2)
    d = w.doc
    d.sel = (int(36.0 * 44100), int(52.0 * 44100))
    w.wave.zoom_all()
    w._on_selection()
    w.set_view_mode("spec")
    _tiles(w, pump)
    w.play()
    pump(0.8)
    w.grab().save(os.path.join(out, f"spectrogram_{theme}.png"))
    w.stop_play()
    w.tabs.setCurrentIndex(1)
    pump(0.2)
    w.set_view_mode("both")
    w.doc.sel = (int(3 * 48000), int(7.5 * 48000))
    w._on_selection()
    _tiles(w, pump)
    pump(0.6)
    w.grab().save(os.path.join(out, f"wave_spectrogram_{theme}.png"))
    w.set_view_mode("wave")
    w.tabs.setCurrentIndex(0)
    pump(0.2)
    d = w.doc
    d.sel = (int(20.5 * 44100), int(31.2 * 44100))
    w._on_selection()
    w.spectrum_dialog()
    dlg = w.spectrum_dlg
    t0 = time.time()
    while dlg.result is None and time.time() - t0 < 20:
        pump(0.1)
    dlg.resize(1000, 600)
    dlg.plot.hover_x = dlg.plot.x_of(554.0)
    pump(0.2)
    dlg.grab().save(os.path.join(out, f"spectrum_{theme}.png"))
    dlg.close()
    w.set_view_mode("spec")
    sd = SpectrogramDialog(w)
    sd.show()
    pump(0.3)
    sd.grab().save(os.path.join(out, f"spectrogram_settings_{theme}.png"))
    sd.reject()
    w.set_view_mode("wave")
    w.record_toggle()
    rd = w.rec_dlg
    rd.marker()
    w.record_toggle()
    pump(1.6)
    rd.marker()
    pump(0.6)
    rd.grab().save(os.path.join(out, f"record_{theme}.png"))
    rd.stop()
    rd.reject()
    pump(0.2)
    w.grab().save(os.path.join(out, f"recorded_{theme}.png"))


def shoot_20(w, out: str, theme: str, pump, lang: str) -> None:
    """2.0: the listening volume on the transport (while playing, the meters still at the file's level) and the
    Record window's «Listen to the input»."""
    from PySide6.QtWidgets import QWidget
    w.tabs.setCurrentIndex(0)
    pump(0.2)
    w.vol.slider.setValue(-140)
    w.play()
    pump(1.0)
    w.grab().save(os.path.join(out, f"volume_{theme}.png"))
    bar = w.findChild(QWidget, "transport")
    bar.grab().save(os.path.join(out, f"volume_transport_{theme}.png"))
    w.vol.button.click()
    pump(0.3)
    bar.grab().save(os.path.join(out, f"volume_muted_{theme}.png"))
    w.vol.button.click()
    w.stop_play()
    w.vol.slider.setValue(0)
    w.record_toggle()
    pump(0.3)
    rd = w.rec_dlg
    rd.mon_on.setChecked(True)
    pump(0.6)
    rd.grab().save(os.path.join(out, f"record_monitor_{theme}.png"))
    rd.mon_on.setChecked(False)
    rd.reject()
    pump(0.2)
    shoot_film(w, out, theme, pump, lang)


def shoot_film(w, out: str, theme: str, pump, lang: str) -> None:
    """2.0 block 1: Return to video, the automatic sync with its result, the channel matrix, the dialogue boost, the
    video window (still frames: no libmpv offscreen), the Film menu."""
    from sound import filmui, fxui, media, sync
    film_path = os.path.join(TMP, "Фильм (2024).mkv")
    if not os.path.exists(film_path):
        ff("-f", "lavfi", "-i", "testsrc2=s=640x272:r=24000/1001:d=12", "-f", "lavfi", "-i", SONG,
           "-filter_complex", "[1]pan=5.1|c0=c0|c1=c1|c2=0.7*c0+0.7*c1|c3=0.3*c0|c4=0.5*c0|c5=0.5*c1[a]",
           "-map", "0:v", "-map", "[a]", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-c:a:0", "ac3",
           "-c:a:1", "aac", "-ac:a:1", "2", "-metadata:s:a:0", "language=rus", "-metadata:s:a:0", "title=Дубляж",
           "-metadata:s:a:1", "language=eng", "-metadata:s:a:1", "title=Original", "-disposition:a:0", "default",
           film_path)
    d = media.open_file(film_path, w.session.folder(), 0)
    d.add_marker(int(2.0 * d.rate), "Сцена 1" if lang == "ru" else "Scene 1")
    w.add_doc(d)
    pump(0.3)
    rd = filmui.ReturnDialog(d, w.cfg, w)
    rd.show()
    rd.stereo.setChecked(True)
    pump(0.2)
    rd.grab().save(os.path.join(out, f"return_video_{theme}.png"))
    rd.reject()
    ad = filmui.AutoSyncDialog(w, d)
    ad.show()
    est = sync.Estimate(offset=-0.4213, L=25 / (24000 / 1001), confidence=0.93, peak=41.0, windows=14, agree=13,
                        residual_ms=4.0)
    ad.show_estimate(est)
    pump(0.2)
    ad.grab().save(os.path.join(out, f"auto_sync_{theme}.png"))
    ad.reject()
    for key in ("chmatrix", "dialog"):
        dlg = fxui.EffectDialog(w, key, d, 0, d.frames, True)
        if key == "chmatrix":
            for k, v in dlg.eff.presets["cm_p_swap_c_lfe"].items():
                dlg._set(k, v)
        dlg.show()
        pump(0.3)
        dlg.grab().save(os.path.join(out, f"fx_{key}_{theme}.png"))
        dlg.reject()
    w.video_window_toggle()
    vw = w.video_win
    d.cursor = int(5.0 * d.rate)
    t0 = time.time()
    while getattr(vw.pic, "frames", 1) < 1 and time.time() - t0 < 10:
        pump(0.1)
    pump(0.3)
    vw.grab().save(os.path.join(out, f"video_window_{theme}.png"))
    vw.close()
    pump(0.1)
    menu = next(a.menu() for a in w.menuBar().actions() if a.menu() is not None and a.text() in ("Фильм", "Film"))
    menu.popup(w.mapToGlobal(w.rect().topLeft()))
    pump(0.3)
    menu.grab().save(os.path.join(out, f"film_menu_{theme}.png"))
    menu.hide()
    shoot_restore(w, out, theme, pump, lang)


def shoot_restore(w, out: str, theme: str, pump, lang: str) -> None:
    """2.0 block 2: the Restoration menu; the hum removal window with the hum found; the clipping repair window with
    the clipping found (marked on the wave behind it); the de-esser while it previews; crackle removal; adaptive noise
    reduction; spectral editing — a selection drawn on the spectrogram (a rectangle and a brush stroke) with its
    window."""
    from sound import fxui, media
    from sound.spectral import Shape, SpecSel
    menu = next(a.menu() for a in w.menuBar().actions() if a.menu() is not None and
                a.text() in ("Реставрация", "Restoration"))
    menu.popup(w.mapToGlobal(w.rect().topLeft()))
    pump(0.3)
    menu.grab().save(os.path.join(out, f"restore_menu_{theme}.png"))
    menu.hide()
    path = os.path.join(TMP, "restore.wav")
    if not os.path.exists(path):
        ff("-f", "lavfi", "-i", "aevalsrc='clip(0.75*sin(2*PI*220*t)*(0.6+0.4*sin(2*PI*0.3*t))"
           "+0.25*sin(2*PI*660*t)*exp(-mod(t,0.5)*4)+0.04*sin(2*PI*50.1*t)+0.025*sin(2*PI*150.3*t)"
           "+0.18*sin(2*PI*3100*t)*between(mod(t,4),1.5,2.6)+0.01*(random(0)-0.5),-0.8,0.8)':s=48000:d=12:c=stereo",
           "-ac", "2", "-c:a", "pcm_f32le", path)
    d = media.open_file(path, w.session.folder())
    w.add_doc(d)
    pump(0.3)
    for key in ("dehum", "declip", "deess", "decrackle", "nr_auto"):
        dlg = fxui.EffectDialog(w, key, d, 0, d.frames, True)
        dlg.show()
        if key == "dehum":
            t0 = time.time()
            while not dlg.hum_label.text() and time.time() - t0 < 10:
                pump(0.1)
        if key == "declip":
            dlg.b_find.click()
            t0 = time.time()
            while (not dlg.b_find.isEnabled() or not dlg.marks) and time.time() - t0 < 20:
                pump(0.1)
        if key == "deess":
            dlg.set_values({**dlg.values, "freq": 3000.0, "threshold": -36.0})
            dlg.b_preview.setChecked(True)
            pump(1.0)
        pump(0.3)
        dlg.grab().save(os.path.join(out, f"fx_{key}_{theme}.png"))
        if key == "declip":
            w.grab().save(os.path.join(out, f"declip_marks_{theme}.png"))
        dlg.reject()
        pump(0.1)
    w.wave.set_marks(d, [])
    w.set_view_mode("both")
    w.wave.zoom_range(0, int(4.5 * d.rate))
    w.spectral_panel()
    panel = w.se_panel
    panel.set_tool("brush")
    sel = SpecSel(d.rate, w.wave.sg_settings.scale, ())
    sel.add(Shape("rect", [(int(1.4 * d.rate), 1800.0), (int(2.7 * d.rate), 5500.0)], chs=(0, 1)))
    sel.add(Shape("brush", [(int(0.2 * d.rate), 50.0), (int(4.3 * d.rate), 50.0)], rx=0.04 * d.rate, ry=0.03,
                  chs=(0,)))
    d.spec_sel = sel
    w.wave.specEdited.emit()
    w.wave.refresh()
    from sound.waveview import GUTTER_W
    part = next(w.wave.spec_part_at(y) for y in range(w.wave.height()) if w.wave.spec_part_at(y) is not None)
    w.wave._hover = (GUTTER_W + 600, part[1] + part[2] * 0.4)
    t0 = time.time()
    while w.wave.sg_pending() and time.time() - t0 < 30:
        pump(0.1)
    pump(0.5)
    w.grab().save(os.path.join(out, f"spectral_{theme}.png"))
    panel.grab().save(os.path.join(out, f"spectral_panel_{theme}.png"))
    panel.close()
    w.set_view_mode("wave")
    d.mark_saved()


def shoot_10(w, out: str, theme: str, pump, lang: str) -> None:
    """1.0: the batch window after a run over a few files (one of them broken), the Hotkeys window, the recovery
    offer, the error window, and the long-film report when there is one (ARTCORE_STRESS_REPORT: report.md)."""
    from sound import batch, crash, recovery
    from sound.dialogs import KeysDialog, RecoveryDialog
    names = ("Вступление", "Песня", "Интервью", "Финал") if lang == "ru" else ("Intro", "Song", "Interview", "Finale")
    src = os.path.join(TMP, f"batch_{theme}", "in")
    os.makedirs(os.path.join(src, "CD2"), exist_ok=True)
    for i, nm in enumerate(names):
        p = os.path.join(src if i < 2 else os.path.join(src, "CD2"), f"{i + 1:02d} {nm}.flac")
        if not os.path.isfile(p):
            ff("-f", "lavfi", "-i", f"sine=f={220 * (i + 1)}:d={6 + 3 * i}:r=44100", "-af",
               f"volume={0.2 + 0.2 * i}", "-ac", "2", "-c:a", "flac", p)
    with open(os.path.join(src, "CD2", "05 broken.wav"), "wb") as f:
        f.write(b"RIFF....WAVEnot a wave" * 30)
    w.batch_window()
    bw = w.batch_win
    job = batch.Job(batch.Source("folder", src, True),
                    [batch.Step("dc"), batch.Step("normalize", {"mode": "loudness", "target": -16.0, "ceiling": -1.0}),
                     batch.Step("comp", {"threshold": -20.0, "ratio": 2.0}, enabled=False),
                     batch.Step("limit", {"ceiling": -1.0}), batch.Step("fades", {"fade_in": 20, "fade_out": 500})],
                    batch.Output("mp3", "320", "other", os.path.join(TMP, f"batch_{theme}", "out"), True,
                                 "{n:2} {name}", ""))
    bw.set_job(job)
    bw.resize(1180, 820)
    bw.tabs.setCurrentIndex(1)
    bw.chain.setCurrentRow(1)
    bw.start()
    t0 = time.time()
    while bw._running() and time.time() - t0 < 120:
        pump(0.1)
    pump(0.4)
    bw.grab().save(os.path.join(out, f"batch_{theme}.png"))
    bw.tabs.setCurrentIndex(2)
    pump(0.2)
    bw.grab().save(os.path.join(out, f"batch_output_{theme}.png"))
    bw.close()
    pump(0.1)
    kd = KeysDialog(w.menuBar(), w)
    kd.resize(660, 760)
    kd.show()
    pump(0.2)
    kd.grab().save(os.path.join(out, f"hotkeys_{theme}.png"))
    kd.close()
    ents = [recovery.Entry("s", "f", {"name": n, "rate": 48000, "frames": int(sec * 48000), "time": time.time() - k * 60,
                                      "pos": st}) for k, (n, sec, st) in
            enumerate(((names[1] + ".flac", 225.4, 14), ("Film 5.1.mkv", 8712.0, 3)))]
    rd = RecoveryDialog(ents, w)
    rd.show()
    pump(0.2)
    rd.grab().save(os.path.join(out, f"recovery_{theme}.png"))
    rd.close()
    try:
        raise ZeroDivisionError("float division by zero")
    except ZeroDivisionError:
        text = crash.report(*sys.exc_info())
    cd = crash.CrashDialog(text, os.path.join(crash.crash_dir(), "crash-20261123-153000.log"), w)
    cd.show()
    pump(0.2)
    cd.grab().save(os.path.join(out, f"crash_{theme}.png"))
    cd.close()
    rep = os.environ.get("ARTCORE_STRESS_REPORT", "")
    if rep and os.path.isfile(rep):
        from PySide6.QtWidgets import QTextBrowser
        tb = QTextBrowser()
        tb.setMarkdown(open(rep, encoding="utf-8").read())
        tb.resize(900, 400)
        tb.show()
        pump(0.1)
        doc = tb.document()
        doc.setTextWidth(tb.viewport().width())
        tb.resize(int(doc.idealWidth()) + 40, int(doc.size().height()) + 30)
        pump(0.3)
        tb.grab().save(os.path.join(out, f"stress_report_{theme}.png"))
        tb.close()


QUICK = "--quick" in sys.argv                    # the empty and main windows and 1.0's only (the 150 % check)

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(main(args[0] if args else "shots", args[1] if len(args) > 1 else "ru"))
