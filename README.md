# ArtCore Sound

Аудиоредактор для Windows: волна по каналам, правка с неограниченной отменой, обработка и эффекты. Шестая программа семейства ArtCore — рядом с [ArtCore Convert](https://github.com/Ruf790/artcore-convert), [ArtCore Player](https://github.com/Ruf790/artcore-player), [ArtCore Music](https://github.com/Ruf790/artcore-music), ArtCore Client и ArtCore Music Remote.

*An audio editor for Windows in the spirit of Sound Forge. English below.*

## Возможности (0.3)

- **Открытие** через FFmpeg: WAV, FLAC, MP3, AAC / M4A, AC-3, E-AC-3, DTS, Opus, OGG, WMA, APE, WavPack и звук из видео (MKV, MP4, AVI, TS …). В файле с несколькими дорожками программа спрашивает, какую открыть (кодек, каналы, язык, название). Файлы и папки можно перетащить в окно; несколько файлов — вкладки.
- **Сохранение:** WAV 16 / 24 / 32 бит float (больше 4 ГБ — RF64), FLAC 16 / 24 бит, MP3 (CBR, VBR V0 / V2), AAC (M4A), Opus, AC-3. «Сохранить» пишет в тот же формат, что был открыт; для звука из видео и DTS спрашивает формат. «Сохранить выделение как…». 16 бит — с дизерингом. MP3 сводится в стерео, AC-3 — в 5.1, частота подгоняется, если формат её не принимает (об этом пишет диалог). Теги переносятся.
- **Волна:** каналы отдельными дорожками (моно, стерео, 5.1, 7.1 — с подписями L R C LFE Ls Rs), масштаб от всего файла до отдельных сэмплов (точки и стебли), линейка времени, шкала dB, обзорная полоса всего файла сверху (рамка — видимая часть, щелчок — переход), отметки клиппинга.
- **Мышь и клавиши:** щелчок — курсор, протяжка — выделение (с автопрокруткой у края), Shift+щелчок — расширить, двойной щелчок — всё; колесо — прокрутка, Ctrl+колесо — масштаб у мыши; ↑ / ↓ — крупнее / мельче, Ctrl+↑ — весь файл, Ctrl+↓ — выделение во весь экран; Home / End, ← / →; поля «Начало / Конец / Длина» редактируются (`1:02.5`, `62.5`, `48000 smp`).
- **Правка:** вырезать, копировать, вставить (поверх выделения или в курсор), вставить как новый файл, удалить, заменить тишиной, вставить тишину, обрезать по выделению. Вставка из файла с другой частотой или числом каналов переводится через FFmpeg (soxr).
- **Отмена и повтор без ограничений** и список «История правок» с переходом на любой шаг. Изменения хранятся на диске: вырезать / вставить / удалить вообще не копируют звук, а обработка записывает только изменённый кусок.
- **Обработка** (выделения или всего файла): громкость; нормализация по пику (sample или true peak) и по громкости EBU R128 — цели −23 / −16 / −14 LUFS или своя, с потолком true peak (если потолок не дал дотянуть до цели, программа об этом скажет); нарастание и затухание (линейное, быстрое, медленное, S-образное), реверс, инверсия, «Убрать DC offset»; «Обрезать тишину в начале и конце» (порог и запас; только правка списка кусков — звук не переписывается); частота дискретизации, каналы (моно / стерео / 5.1), стерео → моно (смешать / только левый / только правый), моно → стерео.
- **Анализ:** в строке состояния — пик, RMS, громкость (LUFS) и клиппинг выделения или всего файла: пик сразу, остальное считается в фоне и запоминается. «Инструменты → Статистика» (Ctrl+I) — таблица по каналам и общая: пик, true peak (4× передискретизация), минимум и максимум, RMS, DC offset, число клиппированных сэмплов; громкость по ITU-R BS.1770-4 / EBU R128 — integrated, диапазон громкости (LRA), максимум momentary и short-term (K-фильтр, блоки 400 мс через 100 мс, пороги −70 LUFS и −10 LU, LFE не учитывается, тылы с весом 1.41). Результаты сверены с `ffmpeg -af ebur128` (расхождение ≤ 0.1 LU). Любая длина, кусками, с отменой.
- **Эффекты** (меню «Эффекты», кнопки «Эквалайзер» и «Шумоподавление» на панели) — к выделению или ко всему файлу, с предпрослушиванием: «Прослушать» играет выделение по кругу через эффект, ползунки меняют звук на ходу, «Обход» включает и выключает эффект для сравнения, индикаторы уровня показывают обработанный звук. Пресеты встроенные и свои. «Применить» — с прогрессом и отменой, одним шагом отмены; фильм 2–3 ч в 5.1 обрабатывается кусками без роста памяти. Любое число каналов.
  - **Параметрический эквалайзер** — 7 полос (срез НЧ / ВЧ, полки, колокола; RBJ-биквады), график АЧХ, точки которого тянутся мышью (колесо — добротность); **графический** — 10 октавных полос ±12 dB.
  - **Компрессор** — порог, степень, мягкое колено, атака, восстановление, усиление на выходе, связь каналов; кривая и индикатор подавления. **Лимитер** — потолок в том числе по true peak, упреждение (look-ahead), восстановление; выход не превышает потолок.
  - **Задержка** — время в мс или в долях такта по BPM, обратная связь, сухой / мокрый, пинг-понг, срез НЧ и ВЧ в петле. **Реверберация** — алгоритмическая (сеть из 8 линий задержки, точное время затухания RT60): размер, затухание, предзадержка, демпфирование ВЧ, ширина, LFE остаётся сухим. У обоих — «Оставить хвост»: эффект продлевается за конец выделения и подмешивается к звуку дальше.
  - **Темп и тон** — темп без смены тона (%), тон без смены темпа (полутоны и центы), с сохранением формант; через Rubber Band в FFmpeg (запасной путь — atempo). Длина меняется; прослушивание сначала просчитывает кусок вперёд.
  - **Шумоподавление** по образцу шума («Взять образец шума» с выделения) — спектральный гейт: подавление, чувствительность, сглаживание по частоте и времени, атака и восстановление; режим «слушать только удаляемый шум». **Удаление щелчков** — поиск по ошибке AR-предсказания, заполнение AR-интерполяцией; «Найти щелчки» показывает число и отмечает их на волне до применения.
- **Воспроизведение** через WASAPI — 5.1 играет как 5.1 на Sound Blaster Z; бегущий курсор (окно следует за ним), по выделению, по кругу, пауза; Пробел — играть / стоп, стоп возвращает курсор. Индикаторы уровня по каналам с удержанием пика и лампой клиппинга.
- **Длинные файлы:** звук держится на диске (memmap float32), пики построены по уровням заранее — фильм 2–3 ч в 5.1 открывается и рисуется без нагрузки на память (тест: 6 мин 5.1 — пик памяти ~7 МБ). Рабочие файлы — в `%TEMP%\ArtCoreSound` (папка меняется в Настройках), удаляются при выходе; после сбоя — при следующем запуске.
- **Оформление** как у ArtCore Convert: тёмная и светлая тема (или как в Windows), классический или современный стиль, иконки Font Awesome, интерфейс на русском и английском. Автообновление через GitHub Releases.

Дальше по плану: 0.4 — спектр выделения и спектрограмма, метки и регионы, запись с микрофона или линейного входа; 1.0 — пакетная обработка папки.

## Установка

Скачайте `ArtCoreSound-Setup-X.Y.Z.exe` в [Releases](https://github.com/Ruf790/artcore-sound/releases) — FFmpeg уже внутри. Есть и переносная версия (`ArtCoreSound-portable.zip`: настройки рядом с программой). Windows 10 / 11, 64 бит.

## Сборка

```
build.bat
```

Нужны Python 3.12 / 3.13 (64 бит) и Inno Setup 6.3+. Скрипт ставит зависимости, скачивает FFmpeg (BtbN, GPL), собирает exe (PyInstaller) и установщик. Тесты: `python -m pytest tests` (нужен ffmpeg в PATH).

## English

An audio editor for Windows: opens anything FFmpeg reads (sound from video included) and saves WAV 16 / 24 / 32 float, FLAC, MP3, AAC, Opus and AC-3. Waveform per channel (mono to 7.1) with a time ruler, a dB scale and an overview strip; zoom from the whole file down to single samples; cut, copy, paste, delete, silence, trim with unlimited undo kept on disk; gain, peak / true-peak and EBU R128 loudness normalization (−23 / −16 / −14 LUFS or custom, with a true-peak ceiling), fades, reverse, invert, DC offset removal, trimming the silence at both ends (piece-table only), sample rate and channel conversion, stereo → mono (mix / left / right) and mono → stereo; effects with a live preview (loop, sliders heard on the fly, bypass, the meters after the effect; built-in and own presets): a 7-band parametric EQ with a draggable response graph and a 10-band graphic EQ, a compressor and a look-ahead limiter with a true-peak ceiling, delay (ms or note values, ping-pong, filters in the loop) and an algorithmic reverb (FDN, exact RT60) with «keep the tail», tempo and pitch through Rubber Band, noise reduction by a noise sample (spectral gate, «listen to the removed noise») and click removal (AR detection and interpolation, clicks marked on the wave) — any channel count, long 5.1 films in chunks; analysis in the status bar (peak, RMS, LUFS, clipping, measured in the background) and Tools → Statistics (per channel: peak, true peak, min / max, RMS, DC, clipped samples; integrated loudness, LRA, max momentary / short-term per BS.1770-4, matching ffmpeg's ebur128 within 0.1 LU); WASAPI playback with a moving cursor, selection and loop playback, and peak meters. Long films in 5.1 stay on disk (memmap) with precomputed peak levels. Dark and light themes, Russian and English.

License: GNU GPL v3. Third-party components: [THIRD-PARTY.md](THIRD-PARTY.md).
