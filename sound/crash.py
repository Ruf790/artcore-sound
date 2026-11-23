"""Unhandled errors (1.0): every exception nobody caught — in the window's thread or in any other — is written to a log
in the settings folder (crash\\crash-<time>.log: the version, Windows, the traceback, the last actions) and shown in a
window with «Copy the report», «Open the folder» and «Continue». The journals of the open documents are written
first, so even if the program then dies the work comes back at the next start. faulthandler catches the crashes
inside C code (Qt, PortAudio, numpy) into crash\\fault.log."""
from __future__ import annotations

import collections
import datetime
import faulthandler
import os
import platform
import sys
import threading
import traceback

from . import APP_TITLE, VERSION

_actions: collections.deque = collections.deque(maxlen=50)
_fault_file = None
_dialog_open = False
_bridge = None
on_crash = []                       # callables run before the window shows (the window writes its journals)


def crash_dir() -> str:
    from . import config
    p = os.path.join(config.data_dir(), "crash")
    os.makedirs(p, exist_ok=True)
    return p


def note(text: str) -> None:
    """An action the user took (a menu command) — the last ones go into the report."""
    _actions.append(f"{datetime.datetime.now():%H:%M:%S} {text}")


def report(exc_type, exc, tb, where: str = "") -> str:
    from . import ff
    lines = [f"{APP_TITLE} {VERSION}", f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
             f"Windows: {platform.platform()}" if sys.platform == "win32" else f"OS: {platform.platform()}",
             f"Python {platform.python_version()}" + (" (frozen)" if getattr(sys, "frozen", False) else "")]
    try:
        import PySide6
        lines.append(f"PySide6 {PySide6.__version__}")
    except Exception:                                            # noqa: BLE001
        pass
    try:
        lines.append(f"ffmpeg {ff.version()}")
    except Exception:                                            # noqa: BLE001
        pass
    if where:
        lines.append(f"Thread: {where}")
    lines += ["", "".join(traceback.format_exception(exc_type, exc, tb)).rstrip(), ""]
    if _actions:
        lines.append("Last actions:")
        lines += [f"  {a}" for a in _actions]
    return "\n".join(lines) + "\n"


def write_log(text: str) -> str:
    path = os.path.join(crash_dir(), f"crash-{datetime.datetime.now():%Y%m%d-%H%M%S-%f}.log")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        return ""
    _trim()
    return path


def _trim(keep: int = 30) -> None:
    try:
        d = crash_dir()
        logs = sorted(n for n in os.listdir(d) if n.startswith("crash-") and n.endswith(".log"))
        for n in logs[:-keep]:
            os.remove(os.path.join(d, n))
    except OSError:
        pass


def handle(exc_type, exc, tb, where: str = "") -> str:
    """Log the exception and show the window (in the GUI thread). → the log's path."""
    if issubclass(exc_type, KeyboardInterrupt):
        return ""
    text = report(exc_type, exc, tb, where)
    path = write_log(text)
    if sys.stderr is not None:                      # None in the windowed exe
        try:
            sys.stderr.write(text)
        except (OSError, ValueError, UnicodeError):
            pass
    for fn in list(on_crash):
        try:
            fn()
        except Exception:                                        # noqa: BLE001
            pass
    if _bridge is not None:
        _bridge.show.emit(text, path)
    return path


def _bridge_class():
    from PySide6.QtCore import QObject, Signal

    class Bridge(QObject):
        show = Signal(str, str)
    return Bridge


def show_dialog(text: str, path: str):
    """The window of an error (one at a time; a second error while it is open only goes to the log)."""
    global _dialog_open
    from PySide6.QtWidgets import QApplication
    if _dialog_open or QApplication.instance() is None:
        return None
    _dialog_open = True
    try:
        dlg = CrashDialog(text, path)
        dlg.exec()
        return dlg
    finally:
        _dialog_open = False


def install() -> None:
    """Hook sys.excepthook and threading.excepthook; faulthandler into crash\\fault.log."""
    global _fault_file, _bridge
    from PySide6.QtCore import Qt

    def hook(t, e, tb):
        handle(t, e, tb)

    def thook(args):
        if args.exc_type is SystemExit:
            return
        handle(args.exc_type, args.exc_value, args.exc_traceback, getattr(args.thread, "name", ""))
    sys.excepthook = hook
    threading.excepthook = thook
    if _bridge is None:
        _bridge = _bridge_class()()
        _bridge.show.connect(show_dialog, Qt.QueuedConnection)
    try:
        _fault_file = open(os.path.join(crash_dir(), "fault.log"), "a", encoding="utf-8")
        _fault_file.write(f"\n--- {APP_TITLE} {VERSION} started {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
        _fault_file.flush()
        faulthandler.enable(_fault_file)
    except (OSError, RuntimeError, ValueError):
        pass


def _dialog_base():
    from PySide6.QtWidgets import QDialog
    return QDialog


class CrashDialog(_dialog_base()):
    def __init__(self, text: str, path: str, parent=None):
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout
        from . import style
        from .i18n import t
        super().__init__(parent)
        self.text, self.path = text, path
        self.setWindowTitle(t("cr_title"))
        self.setMinimumSize(720, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        head = QLabel(t("cr_title"))
        head.setStyleSheet("font-size: 13pt; font-weight: 700;")
        lay.addWidget(head)
        msg = QLabel(t("cr_text", path or crash_dir()))
        msg.setWordWrap(True)
        msg.setObjectName("hint")
        lay.addWidget(msg)
        self.box = QPlainTextEdit(text)
        self.box.setReadOnly(True)
        self.box.setLineWrapMode(QPlainTextEdit.NoWrap)
        lay.addWidget(self.box, 1)
        row = QHBoxLayout()
        self.b_copy = QPushButton(style.icon("fa6s.copy"), t("cr_copy"))
        self.b_copy.clicked.connect(self.copy)
        row.addWidget(self.b_copy)
        b = QPushButton(style.icon("fa6s.folder-open"), t("cr_folder"))
        b.clicked.connect(self._folder)
        row.addWidget(b)
        row.addStretch(1)
        ok = QPushButton(t("cr_continue"))
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        lay.addLayout(row)

    def copy(self) -> None:
        from PySide6.QtWidgets import QApplication
        from .i18n import t
        QApplication.clipboard().setText(self.text)
        self.b_copy.setText(t("cr_copied"))

    def _folder(self) -> None:
        from . import winint
        winint.show_in_explorer(self.path or crash_dir())
