"""RU / EN strings."""
from __future__ import annotations

_lang = "ru"

STRINGS = {
    # menus
    "m_file":         ("Файл", "File"),
    "m_edit":         ("Правка", "Edit"),
    "m_view":         ("Вид", "View"),
    "m_process":      ("Обработка", "Process"),
    "m_transport":    ("Воспроизведение", "Transport"),
    "settings":       ("Настройки", "Settings"),
    "about":          ("О программе", "About"),
    "about_text":     ("<b>ArtCore Sound {0}</b><br>Аудиоредактор: волна по каналам, правка с неограниченной отменой, "
                       "обработка и эффекты. Звук открывается и сохраняется через FFmpeg, играет через WASAPI.<br><br>"
                       "FFmpeg {1}<br><span style='color:gray'>{2}</span><br>Вывод звука: {3}<br>"
                       "Рабочие файлы: {4}<br><br>© 2026 Ruf790 · GNU GPL v3 · "
                       "<a href='https://github.com/Ruf790/artcore-sound'>github.com/Ruf790/artcore-sound</a>",
                       "<b>ArtCore Sound {0}</b><br>An audio editor: waveform per channel, editing with unlimited undo, "
                       "processing and effects. Sound is opened and saved through FFmpeg and plays through WASAPI.<br><br>"
                       "FFmpeg {1}<br><span style='color:gray'>{2}</span><br>Sound output: {3}<br>"
                       "Working files: {4}<br><br>© 2026 Ruf790 · GNU GPL v3 · "
                       "<a href='https://github.com/Ruf790/artcore-sound'>github.com/Ruf790/artcore-sound</a>"),
    "quit":           ("Выход", "Quit"),
    "open":           ("Открыть", "Open"),
    "open_tip":       ("Открыть файл (Ctrl+O)", "Open a file (Ctrl+O)"),
    "recent":         ("Последние файлы", "Recent files"),
    "recent_none":    ("Пусто", "Empty"),
    "recent_clear":   ("Очистить список", "Clear the list"),
    "save":           ("Сохранить", "Save"),
    "save_tip":       ("Сохранить (Ctrl+S)", "Save (Ctrl+S)"),
    "save_as":        ("Сохранить как", "Save as"),
    "save_sel":       ("Сохранить выделение как", "Save the selection as"),
    "close":          ("Закрыть", "Close"),
    "close_all":      ("Закрыть все", "Close all"),
    "new":            ("Новый", "New"),
    "show_file":      ("Показать файл в Проводнике", "Show the file in Explorer"),
    "properties":     ("Свойства файла", "File properties"),
    # edit
    "undo":           ("Отменить", "Undo"),
    "undo_n":         ("Отменить: {0}", "Undo: {0}"),
    "redo":           ("Повторить", "Redo"),
    "redo_n":         ("Повторить: {0}", "Redo: {0}"),
    "history":        ("История правок", "Edit history"),
    "cut":            ("Вырезать", "Cut"),
    "copy":           ("Копировать", "Copy"),
    "paste":          ("Вставить", "Paste"),
    "paste_new":      ("Вставить как новый файл", "Paste as a new file"),
    "delete":         ("Удалить", "Delete"),
    "silence":        ("Заменить тишиной", "Silence"),
    "insert_silence": ("Вставить тишину", "Insert silence"),
    "trim":           ("Обрезать по выделению", "Trim to the selection"),
    "trim_short":     ("Обрезать", "Trim"),
    "select_all":     ("Выделить всё", "Select all"),
    "select_none":    ("Снять выделение", "Select none"),
    "select_to_start": ("Выделить до начала", "Select to the start"),
    "select_to_end":  ("Выделить до конца", "Select to the end"),
    # view
    "zoom_in":        ("Крупнее", "Zoom in"),
    "zoom_out":       ("Мельче", "Zoom out"),
    "zoom_all":       ("Весь файл", "Whole file"),
    "zoom_sel":       ("Выделение во весь экран", "Zoom to the selection"),
    "zoom_samples":   ("До сэмплов (1:1)", "Down to samples (1:1)"),
    "follow":         ("Следовать за курсором воспроизведения", "Follow the play cursor"),
    "go_start":       ("В начало", "Go to the start"),
    "go_end":         ("В конец", "Go to the end"),
    "theme":          ("Тема", "Theme"),
    "theme_dark":     ("Тёмная", "Dark"),
    "theme_light":    ("Светлая", "Light"),
    "theme_auto":     ("Как в Windows", "As Windows"),
    # process
    "p_gain":         ("Громкость", "Volume"),
    "p_normalize":    ("Нормализация", "Normalize"),
    "p_fade_in":      ("Нарастание", "Fade in"),
    "p_fade_out":     ("Затухание", "Fade out"),
    "p_reverse":      ("Реверс", "Reverse"),
    "p_invert":       ("Инверсия", "Invert"),
    "p_resample":     ("Частота дискретизации", "Sample rate"),
    "p_channels":     ("Каналы", "Channels"),
    "p_whole":        ("Выделения нет — обрабатывается весь файл", "No selection — the whole file is processed"),
    "p_norm_title":   ("Нормализация по пику", "Peak normalization"),
    "p_norm_to":      ("Пик до", "Peak to"),
    "p_norm_now":     ("Сейчас пик {0} dBFS", "The peak now is {0} dBFS"),
    "p_gain_title":   ("Громкость", "Volume"),
    "p_gain_by":      ("Изменить на", "Change by"),
    "p_fade_curve":   ("Форма", "Curve"),
    "curve_linear":   ("Линейная", "Linear"),
    "curve_log":      ("Быстрое начало", "Fast start"),
    "curve_exp":      ("Медленное начало", "Slow start"),
    "curve_scurve":   ("S-образная", "S-curve"),
    "p_silent":       ("Здесь тишина — нормализовать нечего", "This is silence — nothing to normalize"),
    "rs_title":       ("Частота дискретизации", "Sample rate"),
    "rs_rate":        ("Новая частота", "New rate"),
    "rs_note":        ("Пересчёт через FFmpeg (soxr, высокое качество). Весь файл.",
                       "Resampled by FFmpeg (soxr, high quality). The whole file."),
    "chn_title":      ("Каналы", "Channels"),
    "chn_to":         ("Сделать", "Make"),
    "chn_note":       ("Сведение и разведение каналов — матрица FFmpeg. Весь файл.",
                       "Downmix / upmix with FFmpeg's matrix. The whole file."),
    # transport
    "play":           ("Воспроизвести", "Play"),
    "play_tip":       ("Воспроизвести / стоп (Пробел)", "Play / stop (Space)"),
    "play_all":       ("Воспроизвести весь файл", "Play the whole file"),
    "pause":          ("Пауза", "Pause"),
    "stop":           ("Стоп", "Stop"),
    "loop":           ("По кругу", "Loop"),
    "rewind":         ("Назад", "Rewind"),
    "forward":        ("Вперёд", "Forward"),
    "record":         ("Запись (в версии 0.4)", "Record (in version 0.4)"),
    "sel_start":      ("Начало", "Start"),
    "sel_end":        ("Конец", "End"),
    "sel_len":        ("Длина", "Length"),
    "sel_none":       ("—", "—"),
    "meters":         ("Уровни", "Levels"),
    "meter_reset":    ("Щелчок — сбросить пики", "Click resets the peaks"),
    # documents, status
    "empty_title":    ("Перетащите звуковой файл сюда", "Drop a sound file here"),
    "empty_hint":     ("или Файл → Открыть (Ctrl+O). WAV, FLAC, MP3, AAC, AC-3, DTS, Opus, OGG, WMA и звук из видео.",
                       "or File → Open (Ctrl+O). WAV, FLAC, MP3, AAC, AC-3, DTS, Opus, OGG, WMA and sound from video."),
    "untitled":       ("Без имени {0}", "Untitled {0}"),
    "opening":        ("Открываю {0}…", "Opening {0}…"),
    "saving":         ("Сохраняю {0}…", "Saving {0}…"),
    "processing":     ("{0}…", "{0}…"),
    "converting":     ("Перевожу вставку в формат файла…", "Converting the clip to the file's format…"),
    "cancel":         ("Отмена", "Cancel"),
    "cancelled":      ("Отменено", "Cancelled"),
    "busy":           ("Подождите — идёт {0}", "Please wait — {0} is running"),
    "saved":          ("Сохранено: {0}", "Saved: {0}"),
    "open_failed":    ("Не удалось открыть «{0}»:\n{1}", "Could not open «{0}»:\n{1}"),
    "save_failed":    ("Не удалось сохранить «{0}»:\n{1}", "Could not save «{0}»:\n{1}"),
    "op_failed":      ("Не получилось: {0}", "It did not work: {0}"),
    "no_ffmpeg":      ("FFmpeg не найден. Переустановите программу.", "FFmpeg is not found. Reinstall the program."),
    "no_space":       ("Для «{0}» нужно около {1} на диске с рабочими файлами ({2}), свободно {3}. Открыть всё равно?",
                       "«{0}» needs about {1} on the drive of the working files ({2}), {3} is free. Open anyway?"),
    "ask_save":       ("Сохранить изменения в «{0}»?", "Save the changes to «{0}»?"),
    "dont_save":      ("Не сохранять", "Don't save"),
    "already_open":   ("«{0}» уже открыт", "«{0}» is already open"),
    "track_title":    ("Какую дорожку открыть?", "Which track to open?"),
    "track_text":     ("В «{0}» несколько звуковых дорожек:", "«{0}» has several sound tracks:"),
    "track_default":  ("по умолчанию", "default"),
    "files_audio":    ("Звук и видео", "Sound and video"),
    "files_all":      ("Все файлы", "All files"),
    "st_frames":      ("{0} сэмплов", "{0} samples"),
    "st_sel":         ("Выделено {0}", "Selected {0}"),
    "st_disk":        ("Рабочие файлы: {0}", "Working files: {0}"),
    "st_dev":         ("Вывод: {0}", "Output: {0}"),
    "st_null":        ("нет звуковой карты", "no sound card"),
    "fmt_line":       ("{0} Гц · {1} · {2}", "{0} Hz · {1} · {2}"),
    "fmt_float":      ("32 бит float", "32 bit float"),
    "fmt_bits":       ("{0} бит", "{0} bit"),
    "ch_mono":        ("Моно", "Mono"),
    "ch_stereo":      ("Стерео", "Stereo"),
    "ch_n":           ("{0} кан.", "{0} ch"),
    "clip_other":     ("Вставка: {0} Гц, {1} кан. → файл: {2} Гц, {3} кан. Переведу через FFmpeg.",
                       "Clip: {0} Hz, {1} ch → file: {2} Hz, {3} ch. Converting through FFmpeg."),
    "playback_err":   ("Звук не играет: {0}", "No sound: {0}"),
    # undo labels
    "u_open":         ("Открытие", "Open"),
    "u_new":          ("Новый", "New"),
    "u_cut":          ("Вырезать", "Cut"),
    "u_paste":        ("Вставить", "Paste"),
    "u_delete":       ("Удалить", "Delete"),
    "u_silence":      ("Тишина", "Silence"),
    "u_insert_silence": ("Вставка тишины", "Insert silence"),
    "u_trim":         ("Обрезка", "Trim"),
    # save dialog
    "sv_title":       ("Сохранить как", "Save as"),
    "sv_name":        ("Файл", "File"),
    "sv_browse":      ("Обзор…", "Browse…"),
    "sv_format":      ("Формат", "Format"),
    "sv_option":      ("Качество", "Quality"),
    "sv_what":        ("Что", "What"),
    "sv_whole":       ("Весь файл", "The whole file"),
    "sv_selection":   ("Выделение ({0})", "The selection ({0})"),
    "sv_mp3_stereo":  ("MP3 бывает только моно или стерео — каналы будут сведены в стерео.",
                       "MP3 is mono or stereo only — the channels are mixed down to stereo."),
    "sv_ac3_51":      ("AC-3 — не больше 5.1: каналы будут сведены в 5.1.", "AC-3 is 5.1 at most: the channels are mixed down to 5.1."),
    "sv_rate":        ("Частота {0} Гц не подходит формату — будет {1} Гц.", "{0} Hz does not fit the format — it will be {1} Hz."),
    "sv_lossy":       ("Сжатие с потерями: для дальнейшей правки лучше WAV или FLAC.",
                       "Lossy compression: for further editing WAV or FLAC is better."),
    "sv_exists":      ("«{0}» уже есть. Заменить?", "«{0}» already exists. Replace it?"),
    "o_16":           ("16 бит (CD)", "16 bit (CD)"),
    "o_24":           ("24 бит", "24 bit"),
    "o_32f":          ("32 бит float (без потерь при правке)", "32 bit float (no loss while editing)"),
    "o_16:8":         ("16 бит, максимальное сжатие", "16 bit, maximum compression"),
    "o_24:8":         ("24 бит, максимальное сжатие", "24 bit, maximum compression"),
    "o_kbps":         ("{0} кбит/с", "{0} kbit/s"),
    "o_cbr":          ("{0} кбит/с CBR", "{0} kbit/s CBR"),
    "o_v0":           ("VBR V0 (~245 кбит/с)", "VBR V0 (~245 kbit/s)"),
    "o_v2":           ("VBR V2 (~190 кбит/с)", "VBR V2 (~190 kbit/s)"),
    # settings
    "set_title":      ("Настройки", "Settings"),
    "set_lang":       ("Язык", "Language"),
    "set_theme":      ("Тема", "Theme"),
    "set_look":       ("Стиль", "Style"),
    "look_classic":   ("Классический", "Classic"),
    "look_modern":    ("Современный", "Modern"),
    "set_device":     ("Вывод звука", "Sound output"),
    "set_device_default": ("По умолчанию Windows", "Windows default"),
    "set_latency":    ("Задержка", "Latency"),
    "lat_low":        ("Низкая", "Low"),
    "lat_high":       ("Надёжная (если звук прерывается)", "Safe (if the sound stutters)"),
    "set_return":     ("Стоп возвращает курсор туда, откуда начали", "Stop puts the cursor back where playback started"),
    "set_temp":       ("Папка рабочих файлов", "Working files folder"),
    "set_temp_default": ("Временная папка Windows", "Windows temp folder"),
    "set_temp_note":  ("Звук открытых файлов и шаги отмены хранятся на диске: фильм 3 ч в 5.1 — около 12 ГБ.",
                       "The sound of open files and the undo steps live on disk: a 3 h 5.1 film takes about 12 GB."),
    "set_updates":    ("Проверять обновления при запуске", "Check for updates at start"),
    "lang_restart":   ("Язык и тема сменятся после перезапуска программы.", "The language and theme change after a restart."),
    # updates
    "upd_check_now":  ("Проверить обновления", "Check for updates"),
    "upd_title":      ("Обновление ArtCore Sound", "ArtCore Sound update"),
    "upd_available":  ("Доступна версия {0}", "Version {0} is available"),
    "upd_text":       ("Новая версия {0} (у вас {1}). Что нового:", "New version {0} (you have {1}). What's new:"),
    "upd_restart":    ("Программа закроется, обновится и запустится снова.", "The program closes, updates and starts again."),
    "upd_install":    ("Обновить", "Update"),
    "upd_later":      ("Позже", "Later"),
    "upd_downloading": ("Загрузка: {0} из {1} МБ", "Downloading: {0} of {1} MB"),
    "upd_error":      ("Не удалось проверить или скачать обновление: {0}", "Could not check or download the update: {0}"),
    "upd_none":       ("У вас последняя версия ({0}).", "You have the latest version ({0})."),
    "upd_unsaved":    ("Есть несохранённые изменения. Сначала сохраните их.", "There are unsaved changes. Save them first."),
    # common
    "ok":             ("OK", "OK"),
    "seconds":        ("с", "s"),
    "length":         ("Длина", "Length"),
}


def set_lang(lang: str) -> None:
    global _lang
    _lang = "en" if lang == "en" else "ru"


def lang() -> str:
    return _lang


def t(key: str, *args) -> str:
    pair = STRINGS.get(key)
    if pair is None:
        return key
    s = pair[0] if _lang == "ru" else pair[1]
    return s.format(*args) if args else s


def fmt_bytes(n: float) -> str:
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ") if _lang == "ru" else ("B", "KB", "MB", "GB", "TB")
    n = float(n)
    for u in units:
        if abs(n) < 1024 or u == units[-1]:
            return f"{n:.0f} {u}" if u == units[0] else f"{n:.1f} {u}"
        n /= 1024
    return str(n)
