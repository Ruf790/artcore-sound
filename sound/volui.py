"""The listening volume on the transport (2.0): a speaker button (mute / unmute) and a slider in dB, from silence
(VOL_MIN_DB) to 0 dB. It changes only what is heard (player.Volume), never the file; the meters keep showing the
file's own level. The wheel over it steps by 1 dB; the tip tells the value in dB and in %."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QSlider, QToolButton, QWidget

from . import style
from .i18n import t
from .player import VOL_MAX_DB, VOL_MIN_DB, Volume

STEP_DB = 2.0                  # the keys and the menu
WHEEL_DB = 1.0


def vol_text(v: Volume) -> str:
    """«−12.0 dB (25 %)», «без звука»."""
    if v.muted:
        return t("vol_muted")
    if v.db <= VOL_MIN_DB:
        return t("vol_silent")
    pct = round(100 * 10 ** (v.db / 20))
    return t("vol_value", f"{v.db:+.1f}".replace("+0.0", "0.0").replace("-", "−"), pct)


def vol_icon(v: Volume) -> str:
    if v.muted or v.db <= VOL_MIN_DB:
        return "fa6s.volume-xmark"
    if v.db < -20:
        return "fa6s.volume-off"
    if v.db < -6:
        return "fa6s.volume-low"
    return "fa6s.volume-high"


class _Slider(QSlider):
    def wheelEvent(self, ev):                       # 1 dB a notch, whatever the platform's wheel lines are
        steps = ev.angleDelta().y() / 120.0
        if steps:
            self.setValue(int(round(self.value() + steps * WHEEL_DB * 10)))
        ev.accept()


class VolumeControl(QWidget):
    """changed() after every change (the window then saves the config a little later)."""
    changed = Signal()

    def __init__(self, volume: Volume, parent=None):
        super().__init__(parent)
        self.volume = volume
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(2)
        self.button = QToolButton()
        self.button.setObjectName("ctl")
        self.button.clicked.connect(self.toggle_mute)
        self.slider = _Slider(Qt.Horizontal)
        self.slider.setObjectName("listenVol")
        self.slider.setRange(int(VOL_MIN_DB * 10), int(VOL_MAX_DB * 10))      # tenths of a dB
        self.slider.setSingleStep(10)
        self.slider.setPageStep(int(STEP_DB * 30))
        self.slider.setFixedWidth(104)
        self.slider.setFocusPolicy(Qt.NoFocus)                                  # the arrows stay the cursor's
        self.slider.valueChanged.connect(self._slid)
        h.addWidget(self.button)
        h.addWidget(self.slider)
        self.refresh()

    def _slid(self, v: int) -> None:
        if abs(v / 10.0 - self.volume.db) < 0.05:
            return
        self.volume.set(v / 10.0, muted=False if v / 10.0 > VOL_MIN_DB else None)
        self.refresh()
        self.changed.emit()

    def toggle_mute(self) -> None:
        if self.volume.db <= VOL_MIN_DB and not self.volume.muted:
            self.volume.set(-20.0)                  # unmuting a slider at the bottom: back to something audible
        else:
            self.volume.set(muted=not self.volume.muted)
        self.refresh()
        self.changed.emit()

    def step(self, ddb: float) -> None:
        self.volume.step(ddb)
        self.refresh()
        self.changed.emit()

    def refresh(self) -> None:
        v = self.volume
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(v.db * 10)))
        self.slider.blockSignals(False)
        self.button.setIcon(style.icon(vol_icon(v), "#e5484d" if v.muted else ""))
        tip = t("vol_tip", vol_text(v))
        self.slider.setToolTip(tip)
        self.button.setToolTip(tip + "\n" + (t("vol_unmute") if v.muted else t("vol_mute")))
