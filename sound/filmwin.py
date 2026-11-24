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

    # ── the Film menu ─────────────────────────────────────────
    def build_film_menu(self, mb, A) -> None:
        m = mb.addMenu(t("m_film"))
        self.a_video = A(m, "fa6s.tv", t("vw_title"), self.video_window_toggle, "F8")
        self.a_video.setCheckable(True)
        m.addSeparator()
        self.a_shift = A(m, "fa6s.arrows-left-right", t("sy_shift") + "…", self.shift_dialog)
        self.a_speed = A(m, "fa6s.gauge", t("sy_speed") + "…", self.speed_dialog)
        self.a_two = A(m, "fa6s.left-right", t("sy_two") + "…", self.two_points_dialog)
        self.a_autosync = A(m, "fa6s.wand-magic-sparkles", t("sy_auto") + "…", self.auto_sync_dialog,
                            "Ctrl+Shift+Y")
        m.addSeparator()
        self.a_matrix = A(m, "fa6s.table-cells", t("cm_title") + "…", lambda: self.effect_dialog("chmatrix"))
        self.a_dialog = A(m, "fa6s.comment", t("db_title") + "…", lambda: self.effect_dialog("dialog"))
        self.a_mixdown = A(m, "fa6s.shuffle", t("mx_title") + "…", self.channels_dialog)
        m.addSeparator()
        self.a_extract = A(m, "fa6s.file-export", t("ex_title") + "…", self.extract_dialog)
        self.a_assemble = A(m, "fa6s.object-group", t("as_title") + "…", self.assemble_dialog)
        self.film_actions = (self.a_shift, self.a_speed, self.a_two, self.a_autosync, self.a_matrix, self.a_dialog,
                             self.a_mixdown, self.a_extract)

    def _fps_items(self):
        from .sync import FPS
        return [(f"{name} {t('sy_fps')}", f) for name, f in FPS]

    # ── the video window ──────────────────────────────────────
    def video_window_toggle(self) -> None:
        if self.video_win is not None:
            self.video_win.close()
            return
        from .videoui import VideoWindow
        self.video_win = VideoWindow(self)
        self.video_win.closed.connect(self._video_closed)
        self.video_win.show()
        self.a_video.setChecked(True)
        d = self.doc
        if d is not None and not d.film.get("path"):
            self._toast(t("vw_no_film"))

    def _video_closed(self) -> None:
        self.video_win = None
        self.a_video.setChecked(False)

    # ── shift ─────────────────────────────────────────────────
    def shift_dialog(self) -> None:
        from . import sync
        from .dialogs import ValueDialog
        d = self.doc
        if d is None or self._busy() or d.frames <= 0:
            return
        o = self.cfg.get("sync_shift") or {}
        dlg = ValueDialog(t("sy_shift"), self, t("sy_shift_note"))
        amount = dlg.spin(t("sy_amount"), float(o.get("amount", 0)), -36000000, 36000000, "", 3, 1.0)
        unit = dlg.combo(t("sy_unit"), [(t("un_ms"), "ms"), (t("sy_frames"), "frames")], o.get("unit", "ms"))
        fps = dlg.combo(t("sy_fps_of"), self._fps_items(), None)
        f0 = self._film_fps()
        fps.setCurrentIndex(min(range(fps.count()), key=lambda i: abs(fps.itemData(i) - f0)))
        if not dlg.exec() or not amount.value():
            return
        self.cfg["sync_shift"] = {"amount": amount.value(), "unit": unit.currentData()}
        config.save(self.cfg)
        d = self._edit_doc()
        if d is None:
            return
        n = (sync.ms_to_frames(amount.value(), d.rate) if unit.currentData() == "ms" else
             sync.video_frames_to_frames(amount.value(), fps.currentData(), d.rate))
        sync.shift(d, n, t("sy_shift_label", f"{n / d.rate * 1000:+.0f}"))
        self._edited()
        self._toast(t("sy_shifted", f"{n / d.rate * 1000:+.1f}"))

    # ── speed ─────────────────────────────────────────────────
    def speed_dialog(self) -> None:
        from . import sync
        from .dialogs import ValueDialog
        d = self.doc
        if d is None or self._busy() or d.frames <= 0:
            return
        o = self.cfg.get("sync_speed") or {}
        dlg = ValueDialog(t("sy_speed"), self, t("sy_speed_note"))
        items = [(t("sy_speed_item", n, f"{a / b:.6f}"), n) for n, a, b in sync.SPEEDS] + [(t("sy_custom"), "custom")]
        which = dlg.combo(t("sy_speed_what"), items, o.get("which", sync.SPEEDS[0][0]))
        custom = dlg.spin(t("sy_factor"), float(o.get("factor", 1.0)), 0.5, 2.0, "", 6, 0.0001)
        pitch = dlg.combo(t("sy_pitch"), [(t("sy_pitch_film"), False), (t("sy_pitch_keep"), True)],
                          bool(o.get("keep_pitch", False)))
        if not dlg.exec():
            return
        key = which.currentData()
        L = custom.value() if key == "custom" else next(a / b for n, a, b in sync.SPEEDS if n == key)
        keep = bool(pitch.currentData())
        self.cfg["sync_speed"] = {"which": key, "factor": custom.value(), "keep_pitch": keep}
        config.save(self.cfg)
        if abs(L - 1.0) < 1e-9:
            return
        self._retime(L, 0, keep, t("sy_speed_label", sync.speed_name(L)))

    def _retime(self, L: float, lead: int, keep_pitch: bool, label: str) -> None:
        from . import sync
        d = self._edit_doc()
        if d is None:
            return

        def work(pr):
            return sync.retime(d, L, keep_pitch, lead, pr)

        def done(src, err):
            if err:
                if err != "cancel":
                    QMessageBox.warning(self, APP_TITLE, t("op_failed", err))
                return
            sync.apply_retime(d, src, L, lead, label)
            self.wave.zoom_all()
            self._edited()
            self._toast(t("sy_retimed", sync.speed_name(L), f"{lead / d.rate * 1000:+.0f}"))
        self._make_room(d, int(d.frames * L) + max(0, lead))
        self.run_job(t("processing", label), work, done, d)

    # ── two points ────────────────────────────────────────────
    def two_points_dialog(self) -> None:
        from . import sync
        from .dialogs import ValueDialog
        from .doc import fmt_time
        d = self.doc
        if d is None or self._busy() or d.frames <= 0:
            return
        pts = sorted(d.marker_points())
        if len(pts) < 2:
            QMessageBox.information(self, APP_TITLE, t("sy_two_need"))
            return
        dlg = ValueDialog(t("sy_two"), self, t("sy_two_note"))
        items = [(fmt_time(p, d.rate), p) for p in pts]
        m1 = dlg.combo(t("sy_two_m1"), items, pts[0])
        t1 = dlg.time_field(t("sy_two_t1"), pts[0], d.rate)
        m2 = dlg.combo(t("sy_two_m2"), items, pts[-1])
        t2 = dlg.time_field(t("sy_two_t2"), pts[-1], d.rate)
        pitch = dlg.combo(t("sy_pitch"), [(t("sy_pitch_film"), False), (t("sy_pitch_keep"), True)], False)
        m1.currentIndexChanged.connect(lambda _i: t1.setText(fmt_time(m1.currentData(), d.rate)))
        m2.currentIndexChanged.connect(lambda _i: t2.setText(fmt_time(m2.currentData(), d.rate)))
        if not dlg.exec():
            return
        a, b = dlg.frames_of(t1), dlg.frames_of(t2)
        if a is None or b is None:
            return
        try:
            L, lead = sync.two_points(m1.currentData(), m2.currentData(), a / d.rate, b / d.rate, d.rate)
        except ValueError as e:
            QMessageBox.warning(self, APP_TITLE, t("sy_two_bad", str(e)))
            return
        if abs(L - 1.0) < 1e-9:
            sync.shift(self._edit_doc(), lead, t("sy_shift_label", f"{lead / d.rate * 1000:+.0f}"))
            self._edited()
            return
        self._retime(L, lead, bool(pitch.currentData()), t("sy_two_label"))

    # ── automatic sync ────────────────────────────────────────
    def auto_sync_dialog(self) -> None:
        from .filmui import AutoSyncDialog
        d = self.doc
        if d is None or self._busy() or d.frames <= 0:
            return
        dlg = AutoSyncDialog(self, d)
        dlg.exec()
        est = dlg.result_est
        if est is None or not dlg.apply_now:
            return
        lead = est.lead(d.rate)
        from . import sync
        if abs(est.L - 1.0) < 1e-7:
            dd = self._edit_doc()
            if dd is None:
                return
            sync.shift(dd, lead, t("sy_auto_label"))
            self._edited()
            self._toast(t("sy_shifted", f"{lead / d.rate * 1000:+.1f}"))
        else:
            self._retime(est.L, lead, dlg.keep_pitch.isChecked(), t("sy_auto_label"))

    # ── channels out, 5.1 in ──────────────────────────────────
    def extract_dialog(self) -> None:
        from . import chans, media
        from .filmui import ExtractDialog
        d = self.doc
        if d is None or self._busy() or d.frames <= 0:
            return
        dlg = ExtractDialog(d, self.cfg, self)
        if not dlg.exec():
            return
        config.save(self.cfg)
        jobs = dlg.jobs()                       # [(channel, path)]
        fmt, opt = dlg.format()

        def work(pr):
            from .doc import Document
            out = []
            for n, (c, path) in enumerate(jobs):
                src = chans.extract(d, c, media.Progress(lambda f, n=n: pr((n + f * 0.5) / len(jobs))))
                one = Document(rate=d.rate, channels=1, folder=d.folder, labels=("M",), layout="mono")
                one.set_initial(src)
                media.save(one, path, fmt, opt, media.Progress(lambda f, n=n: pr((n + 0.5 + f * 0.5) / len(jobs))),
                           markers=False)
                from .store import delete_source
                delete_source(src)
                out.append(path)
            return out

        def done(res, err):
            if err:
                if err != "cancel":
                    QMessageBox.warning(self, APP_TITLE, t("op_failed", err))
                return
            self._toast(t("ex_done", len(res), os.path.dirname(res[0]) if res else ""))
        self.run_job(t("ex_title"), work, done, d)

    def assemble_dialog(self) -> None:
        from . import chans
        from .filmui import AssembleDialog
        if self._busy():
            return
        dlg = AssembleDialog(self.cfg, self)
        if not dlg.exec():
            return
        config.save(self.cfg)
        paths = dlg.paths()
        folder = self.session.folder()

        def work(pr):
            return chans.assemble(paths, folder, pr)

        def done(res, err):
            if err:
                from .store import remove_folder
                remove_folder(folder)
                if err != "cancel":
                    QMessageBox.warning(self, APP_TITLE, t("op_failed", err))
                return
            from .doc import Document
            src, rate = res
            name = os.path.splitext(os.path.basename(paths[0]))[0]
            for suf in ("_L", "-L", ".L", " L", "_FL", "-FL"):
                if name.endswith(suf):
                    name = name[:-len(suf)]
                    break
            doc = Document(rate=rate, channels=6, folder=folder, labels=tuple(ff.labels(6, "5.1")), layout="5.1",
                           name=f"{name} 5.1")
            doc.set_initial(src)
            self.add_doc(doc)
            self._toast(t("as_done", doc.name))
        self.run_job(t("as_title"), work, done, None)

    def _film_fps(self) -> float:
        d = self.doc
        return float((d.film.get("fps") if d is not None else 0) or 0) or 25.0


def fps_text(fps: float) -> str:
    return f"{fps:.3f}".rstrip("0").rstrip(".")


__all__ = ["FilmCommands", "fps_text", "ff"]
