"""Screenshots of the window (offscreen Qt) in the dark and the light theme: the empty window, a stereo file with a
selection while it plays (the status bar measured), the sample-level zoom, a 5.1 file, Tools → Statistics of the
5.1 file, the Normalize dialog. Test sound is made by ffmpeg.

Usage: python tools/screenshots.py OUT_DIR [ru|en]"""
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
        for x in w.docs:
            x.mark_saved()
        w.close()
        s.close()
        pump(0.1)
    print("screenshots in", out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "shots", sys.argv[2] if len(sys.argv) > 2 else "ru"))
