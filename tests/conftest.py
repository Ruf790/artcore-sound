import os
import shutil
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ARTCORE_AUDIO", "null")          # no sound card on CI: the timer output


def ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", *args], check=True)


SIX = "[0]pan=5.1|c0=c0|c1=0.5*c0|c2=0.7*c0|c3=0.3*c0|c4=0.25*c0|c5=0.2*c0"


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory):
    """Real files made by ffmpeg: WAV 16 bit stereo 44.1 k, WAV 24 bit mono, FLAC 24 bit, MP3, M4A (AAC), AC-3 5.1,
    E-AC-3, DTS 5.1, Opus, OGG Vorbis, WMA, a film (H.264 + AC-3 5.1 «rus» + AAC stereo «eng»), a broken file."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is needed")
    d = str(tmp_path_factory.mktemp("media"))
    tone = "sine=f=440:d=3:r=44100"
    ff("-f", "lavfi", "-i", "aevalsrc='0.5*sin(2*PI*440*t)|0.25*sin(2*PI*660*t)':s=44100:d=3", "-c:a", "pcm_s16le",
       os.path.join(d, "stereo16.wav"))
    ff("-f", "lavfi", "-i", "sine=f=1000:d=2:r=48000", "-c:a", "pcm_s24le", os.path.join(d, "mono24.wav"))
    ff("-f", "lavfi", "-i", "aevalsrc='0.4*sin(2*PI*300*t)|0.4*sin(2*PI*500*t)':s=96000:d=2", "-c:a", "flac",
       "-sample_fmt", "s32", "-bits_per_raw_sample", "24", os.path.join(d, "hires.flac"))
    ff("-f", "lavfi", "-i", tone, "-ac", "2", "-c:a", "libmp3lame", "-b:a", "192k", os.path.join(d, "song.mp3"))
    ff("-f", "lavfi", "-i", tone, "-ac", "2", "-c:a", "aac", "-b:a", "192k", os.path.join(d, "song.m4a"))
    ff("-f", "lavfi", "-i", "sine=f=440:d=3:r=48000", "-filter_complex", SIX, "-c:a", "ac3", "-b:a", "448k",
       os.path.join(d, "film.ac3"))
    ff("-f", "lavfi", "-i", "sine=f=440:d=3:r=48000", "-filter_complex", SIX, "-c:a", "eac3", "-b:a", "384k",
       os.path.join(d, "film.eac3"))
    ff("-f", "lavfi", "-i", "sine=f=440:d=3:r=48000", "-filter_complex", SIX, "-c:a", "dca", "-strict", "-2",
       os.path.join(d, "film.dts"))
    ff("-f", "lavfi", "-i", "sine=f=440:d=3:r=48000", "-ac", "2", "-c:a", "libopus", "-b:a", "128k",
       os.path.join(d, "voice.opus"))
    ff("-f", "lavfi", "-i", tone, "-ac", "2", "-c:a", "libvorbis", "-q:a", "5", os.path.join(d, "song.ogg"))
    ff("-f", "lavfi", "-i", tone, "-ac", "2", "-c:a", "wmav2", "-b:a", "128k", os.path.join(d, "song.wma"))
    ff("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=3", "-f", "lavfi", "-i", "sine=f=440:d=3:r=48000",
       "-f", "lavfi", "-i", "sine=f=880:d=3:r=48000", "-filter_complex", "[1]pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|"
       "c5=c0[a]", "-map", "0:v", "-map", "[a]", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast",
       "-c:a:0", "ac3", "-c:a:1", "aac", "-ac:a:1", "2", "-metadata:s:a:0", "language=rus",
       "-metadata:s:a:0", "title=Дубляж", "-metadata:s:a:1", "language=eng", "-disposition:a:0", "default",
       os.path.join(d, "Фильм (2024).mkv"))
    with open(os.path.join(d, "broken.wav"), "wb") as f:
        f.write(b"RIFF....WAVEnot really" * 50)
    return d


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTCORE_SOUND_DATA", str(tmp_path / "data"))
    return str(tmp_path / "data")


@pytest.fixture()
def session(tmp_path):
    from sound import store
    s = store.Session(str(tmp_path / "work"))
    yield s
    s.close()


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from sound import style
    app.setStyle("Fusion")
    app.setStyleSheet(style.stylesheet())
    yield app
    from PySide6.QtCore import QCoreApplication, QEvent
    for w in app.topLevelWidgets():
        w.hide()
        w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def pump(app, secs: float, until=None):
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until() if until is not None else True
