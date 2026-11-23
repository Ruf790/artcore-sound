"""The main window's film commands (2.0), kept out of window.py: File → Return to video, and the Film menu (the video
window, dub sync, the 5.1 tools). MainWindow mixes this in."""
from __future__ import annotations

import os

from PySide6.QtWidgets import QMessageBox

from . import APP_TITLE, config, ff
from .i18n import t


class FilmCommands:
    # ── File → Return to video ────────────────────────────────
    def return_to_video_dialog(self) -> None:
        from . import film
        from .filmui import ReturnDialog
        d = self.doc
        if d is None or self._busy() or d.frames <= 0:
            return
        dlg = ReturnDialog(d, self.cfg, self)
        if not dlg.exec():
            return
        dlg.remember()
        config.save(self.cfg)
        opts = dlg.options()
        src = dlg.film_path()
        if not d.film.get("path") or os.path.abspath(src) != d.film.get("path"):
            info = dlg.info
            d.film = {"path": os.path.abspath(src), "track": 0, "index": -1, "fps": info.fps if info else 0}
        if opts.mode == "replace":
            tr = next((x for x in dlg.info.tracks if x.index == opts.replace_index), None)
            if tr is not None:
                d.film.update(track=tr.pos, index=tr.index)
        if os.path.exists(opts.out_path) and os.path.abspath(opts.out_path) != os.path.abspath(src) and \
                QMessageBox.question(self, APP_TITLE, t("rv_exists", opts.out_path)) != QMessageBox.Yes:
            return
        vw = getattr(self, "video_win", None)
        if vw is not None and os.path.abspath(opts.out_path) == os.path.abspath(src):
            vw.release()                              # Windows: the film must not be open while it is replaced
        info = dlg.info

        def work(progress):
            return film.return_to_video(d, opts, progress, info)

        def done(res, err):
            if err:
                if err != "cancel":
                    QMessageBox.warning(self, APP_TITLE, t("rv_failed", err))
                return
            self._toast(t("rv_done", os.path.basename(res.path), res.track + 1, film.CODECS[opts.codec].name,
                          res.channels, res.streams.get("video", 0), res.streams.get("audio", 0),
                          res.streams.get("subtitle", 0)))
            self.last_video_out = res.path
        self.run_job(t("rv_job", os.path.basename(opts.out_path)), work, done, d)

    def _film_fps(self) -> float:
        d = self.doc
        return float((d.film.get("fps") if d is not None else 0) or 0) or 25.0


def fps_text(fps: float) -> str:
    return f"{fps:.3f}".rstrip("0").rstrip(".")


__all__ = ["FilmCommands", "fps_text", "ff"]
