# Third-party software in ArtCore Sound

ArtCore Sound is free software under the GNU GPL v3 (see LICENSE). It ships with or uses:

| Component | License | Where |
|---|---|---|
| **FFmpeg** (BtbN/FFmpeg-Builds, GPL build: LAME, Opus, Vorbis, soxr, Rubber Band …) | GPL v3 (the build as a whole) | https://ffmpeg.org · https://github.com/BtbN/FFmpeg-Builds |
| **PortAudio** (bundled with python-sounddevice) | MIT-style | http://www.portaudio.com |
| **python-sounddevice** | MIT | https://github.com/spatialaudio/python-sounddevice |
| **Qt 6 / PySide6** | LGPL v3 | https://www.qt.io · https://wiki.qt.io/Qt_for_Python |
| **QtAwesome** + Font Awesome 6 Free | MIT; icons CC BY 4.0 / SIL OFL 1.1 | https://github.com/spyder-ide/qtawesome |
| **NumPy** | BSD 3-Clause | https://numpy.org |
| **SciPy** (the K filter, the true-peak oversampling, the effects' filters) | BSD 3-Clause | https://scipy.org |
| **Rubber Band Library** (inside FFmpeg: the tempo / pitch effect) | GPL v2+ | https://breakfastquay.com/rubberband/ |
| **Python** | PSF License | https://www.python.org |

The FFmpeg source code for the bundled build is available from the links above (the exact build is named in
`ffmpeg/VERSION.txt`); FFmpeg's own license text is in `ffmpeg/LICENSE.txt`.
