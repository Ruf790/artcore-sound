# ArtCore Sound

Аудиоредактор для Windows в духе Sound Forge: волна по каналам до 5.1 и 7.1, правка с неограниченной отменой, обработка, эффекты с предпрослушиванием, реставрация и спектральная правка, звук для фильмов, плагины VST3 и макросы, рип и запись Audio CD, анализ, спектр и спектрограмма, метки и регионы, запись и пакетная обработка. Шестая программа семейства ArtCore — рядом с [ArtCore Convert](https://github.com/Ruf790/artcore-convert), [ArtCore Player](https://github.com/Ruf790/artcore-player), [ArtCore Music](https://github.com/Ruf790/artcore-music), ArtCore Client и ArtCore Music Remote.

*An audio editor for Windows in the spirit of Sound Forge. [English below](#english).*

![Главное окно](docs/screenshots/main_dark.png)

## Возможности

### Файлы
- **Открытие** через FFmpeg: WAV, FLAC, MP3, AAC / M4A, AC-3, E-AC-3, DTS, TrueHD, Opus, OGG, WMA, APE, WavPack, ALAC и звук из видео (MKV, MP4, AVI, TS, M2TS, MOV …). В файле с несколькими дорожками программа спрашивает, какую открыть (кодек, каналы, язык, название). Файлы и папки можно перетащить в окно, несколько файлов открываются вкладками. «Открыть с помощью → ArtCore Sound» в Проводнике для всех этих форматов, а также «Открыть в ArtCore Sound» из [ArtCore Convert](https://github.com/Ruf790/artcore-convert) и [ArtCore Music](https://github.com/Ruf790/artcore-music).
- **Сохранение:** WAV 16 / 24 / 32 бит float (больше 4 ГБ — RF64), FLAC 16 / 24 бит, MP3 (CBR, VBR V0 / V2), AAC (M4A), Opus, AC-3. «Сохранить» пишет в тот формат, что был открыт; для звука из видео и DTS спрашивает. «Сохранить выделение как…», «Сохранить регионы как отдельные файлы…». 16 бит — с дизерингом; MP3 сводится в стерео, AC-3 — в 5.1, частота подгоняется под формат (диалог об этом пишет). Теги, метки и главы переносятся. Запись идёт через временный файл — старый файл цел, если что-то пошло не так.

### Волна и правка
- **Волна:** каналы отдельными дорожками (моно, стерео, 5.1, 7.1 — с подписями L R C LFE Ls Rs), масштаб от всего файла до отдельных сэмплов (точки и стебли), линейка времени, шкала dB, обзорная полоса всего файла, отметки клиппинга. На экранах 125 / 150 % волна рисуется с колонкой на каждый физический пиксель.
- **Мышь и клавиши:** щелчок — курсор, протяжка — выделение (с автопрокруткой), Shift+щелчок — расширить, двойной щелчок — всё; колесо — прокрутка, Ctrl+колесо — масштаб у мыши; поля «Начало / Конец / Длина» редактируются (`1:02.5`, `62.5`, `48000 smp`). Полный список — в окне «Горячие клавиши» (F1) и [ниже](#горячие-клавиши).
- **Правка:** вырезать, копировать, вставить (поверх выделения или в курсор), вставить как новый файл, удалить, заменить тишиной, вставить тишину, обрезать по выделению. Вставка из файла с другой частотой или числом каналов переводится через FFmpeg (soxr).
- **Отмена и повтор без ограничений**, список «История правок» с переходом на любой шаг. Изменения хранятся на диске: вырезать / вставить / удалить не копируют звук, обработка записывает только изменённый кусок. Когда ветка «Повторить» отбрасывается новой правкой, её файлы сразу удаляются; если для длинной операции не хватает места, программа сама забывает шаги «Повторить» и скажет об этом.

### Обработка и эффекты
- **Обработка** (выделения или всего файла): громкость; нормализация по пику (sample или true peak) и по громкости EBU R128 — цели −23 / −16 / −14 LUFS или своя, с потолком true peak; нарастание и затухание (линейное, быстрое, медленное, S-образное), реверс, инверсия, DC offset; обрезка тишины в начале и конце; частота дискретизации, каналы (моно / стерео / 5.1), стерео → моно (смешать / левый / правый), моно → стерео.
- **Эффекты с предпрослушиванием:** «Прослушать» играет выделение по кругу через эффект, ползунки меняют звук на ходу, «Обход» сравнивает с исходным, индикаторы показывают обработанный звук. Пресеты встроенные и свои. Любое число каналов, фильм 2–3 ч в 5.1 обрабатывается кусками без роста памяти.
  - **Параметрический эквалайзер** (7 полос, график с точками, которые тянутся мышью) и **графический** (10 октавных полос).
  - **Компрессор** (порог, степень, мягкое колено, атака, восстановление, связь каналов) и **лимитер** (потолок в том числе по true peak, упреждение).
  - **Задержка** (мс или доли такта по BPM, пинг-понг, фильтры в петле) и **реверберация** (сеть из 8 линий задержки, точное RT60) — с «Оставить хвост».
  - **Темп и тон** через Rubber Band с сохранением формант.
  - **Шумоподавление** по образцу шума (спектральный гейт; режим «слушать удаляемый шум»). Образец сохраняется в файл (`.acnoise`) и загружается — для пакета и следующих сеансов. **Удаление щелчков** — поиск по ошибке AR-предсказания и AR-интерполяция, щелчки отмечаются на волне до применения.

### Анализ
- В строке состояния — пик, RMS, громкость (LUFS) и клиппинг выделения или всего файла (считается в фоне). «Инструменты → Статистика» (Ctrl+I): по каналам и общая — пик, true peak (4×), минимум и максимум, RMS, DC, клиппинг; громкость по ITU-R BS.1770-4 / EBU R128 — integrated, LRA, максимум momentary и short-term (сверено с `ffmpeg -af ebur128`, ≤ 0.1 LU).
- **Спектр:** панель внизу (F6) — спектр выделения или живой спектр того, что играет; большое окно (Ctrl+Shift+F) — метод Уэлча по всему выделению, FFT 512…65536, окна Ханна / Блэкмана-Харриса / flat-top, лин / лог, сглаживание 1/3 и 1/6 октавы, частота, уровень и нота под мышью, CSV.
- **Спектрограмма:** вид «Волна / Спектрограмма / Волна + спектрограмма» (Alt+1…3, F7) — тайлами в фоне с кэшем, шкала лин / лог / мел, палитры для обеих тем.

### Метки, регионы, запись
- **Метки и регионы** (M, R): флажки на волне тянутся мышью, список справа, Ctrl+← / →; входят в отмену и сдвигаются при правках. Сохраняются в WAV (`cue` + `LIST/adtl`, как Sound Forge), FLAC / Opus (CHAPTER, CUESHEET), MP3 / M4A (главы); главы MKV / MP4 открываются как регионы. Экспорт и импорт — CSV, .cue, метки Audacity.
- **Запись** (Ctrl+R) со входа WASAPI: индикаторы до записи, пауза, метки, предзапись, старт по уровню, запас диска; поток идёт прямо на диск.
- **Воспроизведение** через WASAPI — 5.1 играет как 5.1; бегущий курсор, по выделению, по кругу; индикаторы по каналам с удержанием пика.
- **Громкость прослушивания (2.0):** ползунок с динамиком на транспорте (колесо мыши — по 1 dB, щелчок по динамику — без звука), Ctrl+Alt+↑ / ↓ / M и пункты в меню «Воспроизведение». Меняет только то, что слышно — воспроизведение, предпрослушивание эффектов, прослушивание входа в окне записи, — плавно и на ходу; файл, сохранение и индикаторы (честный уровень файла) не меняются. Запоминается между запусками. В окне записи — галочка «Слушать вход».

### Звук для фильмов (новое в 2.0)
- **Файл → Вернуть в видео…** (Ctrl+Shift+E): звук заменяет дорожку фильма, из которого открыт, или добавляется новой — видео, остальные дорожки, субтитры и вложения копируются без перекодирования. AC-3, E-AC-3, AAC, FLAC, Opus, PCM, DTS (ядро, экспериментальный кодер FFmpeg); язык, название, «по умолчанию», задержка; главы фильма или из меток и регионов. Пишется во временный файл, проверяется ffprobe, по умолчанию — новый файл рядом.
- **Окно видео** (F8): кадр под курсором, при воспроизведении — видео через libmpv, ведомое часами плеера (без libmpv — кадры через FFmpeg); отдельное окно, можно на второй монитор.
- **Синхронизация дубляжа:** сдвиг в мс или кадрах; скорость PAL 25 ↔ 23.976 / 24 с сохранением тона или «как плёнка»; растяжение по двум меткам; автосинхронизация с эталоном (огибающие атак, взаимная корреляция, сдвиг + скорость + уверенность) — всё одним шагом отмены.
- **5.1:** матрица каналов и усиление диалогов (с предпрослушиванием, пресетами, шагами пакета); 5.1 → стерео по ATSC/ITU, LoRo, Dolby Pro Logic II; апмикс стерео → 5.1; каналы в моно-файлы и 5.1 из шести моно.

### Реставрация (новое в 2.0)
- **Меню «Реставрация»:** образец шума и шумоподавление по нему, **адаптивное шумоподавление** без образца (статистика минимумов, режимы «голос» и «музыка»), удаление щелчков и **треска** (все мелкие импульсы блока одной AR-моделью — пластинки), **восстановление клиппинга** («Найти клиппинг» отмечает срезанные места на волне и подсказывает нужный запас; LSAR-интерполяция, сплайн для длинных участков), **удаление фона 50/60 Гц** с гармониками («Определить» находит частоту, фильтр следит за её дрейфом), **де-эссер** (полоса или весь сигнал, индикатор подавления). Все — с предпрослушиванием, пресетами и шагами пакета.
- **Спектральная правка** (Ctrl+Shift+R): выделение прямо на спектрограмме — время, прямоугольник, кисть, лассо (Shift добавить, Alt вычесть, Ctrl все каналы, Alt+колесо размер кисти); ослабить, стереть, заполнить по соседям или усилить; «Прослушать выделенное / результат / исходник». Считается только задетый кусок — вне выделения звук совпадает до бита.

### Плагины VST3 и макросы (новое в 2.0)
- **Плагины VST3** (через Spotify pedalboard): «Эффекты → Плагины VST3 ▸» — по производителям, сверху последние; «Настройки → VST3» — папки поиска, сканирование, список «Не загрузились» с «Повторить». Окно плагина — как у своих эффектов: параметры, **родное окно плагина**, пресеты (с состоянием плагина) и файлы `.vstpreset`, предпрослушивание, «Обход», «Оставить хвост»; задержка плагина учитывается. 5.1: все каналы сразу, **парами** (L R, C LFE, Ls Rs), каждый отдельно или только выбранные каналы. Шаг пакета для любого плагина.
- **Падение плагина не валит программу:** каждый плагин работает в отдельном процессе (`ArtCoreSound.exe --vst-host`), звук идёт через общую память. Упал или завис — программа говорит об этом, предпрослушивание продолжается без эффекта, документ не меняется; при сканировании такой плагин попадает в «Не загрузились».
- **Макросы** — меню «Макросы»: «Записать макрос» (Ctrl+Shift+M, в строке состояния «● Запись макроса: шагов N») запоминает всё, что делается с файлом: правку, обработку, эффекты и плагины, сохранение — с параметрами. Выделения записываются в секундах от начала или от конца файла, поэтому «срезать первые 2 с» или «затухание последней секунды» подходят к файлам любой длины. Макрос получает название и горячую клавишу, шаги можно переставить, выключить, изменить; применяется к выделению или файлу одним шагом отмены (если шаг не удался, файл не меняется); экспорт и импорт `.acmacro`; шаг пакета «Макрос».

### Audio CD (новое в 2.0)
- **Рип** (Файл → Audio CD → Рип, Ctrl+Shift+D): треки с CD-TEXT или названиями из **MusicBrainz**, правка названий; в файлы любого формата с тегами и шаблоном имени (с подпапками: `{artist} - {album}/{n:2} - {title}`; FLAC/WAV 16 бит — бит в бит), в редактор по трекам или **весь диск одним файлом с регионами**. **Надёжное чтение:** каждый кусок читается дважды, несовпавшие перечитываются, пока два чтения не совпадут; отчёт с CRC трека. Поправка на смещение чтения привода (по списку AccurateRip).
- **Запись** (Файл → Audio CD → Записать): треки из регионов, от каждой метки или весь файл; паузы между треками по выбору; **CD-TEXT** (кириллица латиницей — в CD-TEXT её нет); запись диском целиком (DAO — образ собирается программой) или по трекам; скорость, проверка диска, **стирание CD-RW**, «Сохранить образ .cue/.bin». Рип, записанный обратно без правки, совпадает с оригиналом до бита. Через службу записи Windows (IMAPI2), ничего ставить не нужно.

### Пакетная обработка (новое в 1.0)
![Пакетная обработка](docs/screenshots/batch_dark.png)

«Инструменты → Пакетная обработка…» (Ctrl+B) — отдельное окно, редактор при этом остаётся доступен.
- **Источник:** папка (с подпапками или без, фильтр расширений) или список файлов (перетаскиванием файлов и папок). Для видео — какую дорожку брать: по умолчанию, по языку (`rus, ru`) или все, каждую в свой файл.
- **Цепочка шагов** — всё из меню «Обработка» и «Эффекты»: громкость, нормализация (LUFS с потолком true peak, пик, true peak), DC offset, обрезка тишины, нарастание и затухание по краям (в мс), каналы, частота, эквалайзеры, компрессор, лимитер, задержка, реверберация, темп и тон, шумоподавление по файлу образца шума, удаление щелчков. Шаги добавляются, удаляются, переставляются (кнопками или перетаскиванием), включаются галочкой. Параметры — в тех же окнах эффектов с пресетами, а предпрослушивание идёт по первым 30 секундам первого файла.
- **Сохранение:** формат и качество как в «Сохранить как» (или «как у исходника»); папка та же или другая с сохранением структуры подпапок; имя по шаблону `{name}`, `{n}` / `{n:3}`, `{date}`, `{track}`, `{lang}` плюс суффикс (пример имени показывается сразу); если файл есть — новое имя «имя (2)», пропустить или заменить; теги и метки сохраняются.
- **Пакеты** (источник + цепочка + сохранение) сохраняются как пресеты (JSON, `.acbatch`) и открываются из файла.
- **Выполнение:** в фоне, без вкладок; несколько файлов одновременно — каждый в своём процессе (по умолчанию половина ядер), звук идёт потоком через диск, память почти не растёт, а место на диске резервируется под каждый файл. Прогресс по файлу и общий с оценкой оставшегося времени, пауза, остановка — недописанные файлы удаляются. Битый файл не останавливает пакет. Журнал по каждому файлу: готово / ошибка (понятным языком), LUFS и пик до и после, время; итог; отчёт в CSV (для Excel) или текстом; «Открыть папку».

### Надёжность (новое в 1.0)
- **Восстановление после сбоя.** Звук документа и так на диске, поэтому программа ведёт журнал таблицы кусков, истории отмен, меток и путей (`doc.json` в рабочей папке, атомарно). Если программа упала или её закрыли принудительно, при следующем запуске она предложит восстановить несохранённые документы — со всей историей отмен.
- **Необработанные ошибки** пишутся в лог (`%APPDATA%\ArtCoreSound\crash`: версия, Windows, трассировка, последние действия), а окно «Произошла ошибка» даёт «Скопировать отчёт». Падения внутри библиотек ловит faulthandler.
- **Нехватка места посреди операции:** понятное сообщение, документ не меняется, недописанные рабочие и выходные файлы удаляются.
- Ошибки — человеческим языком: «нет доступа (файл занят)», «файл повреждён», «слишком длинный путь». Длинные пути (больше 260 символов) и имена с кириллицей поддерживаются.

### Длинные файлы
Звук держится на диске (memmap float32), пики построены по уровням заранее, поэтому фильм 2–3 ч в 5.1 открывается, рисуется, правится и обрабатывается без роста памяти. Проверка — `tools/stress_long.py` (фильм 2,5 ч 5.1 с AC-3, E-AC-3 и DTS; в CI — отдельная задача по кнопке):

<!-- stress -->
Фильм 2 ч 30 мин, 5.1, 48 кГц (MKV: видео, AC-3 640k, E-AC-3, DTS, 15 глав) на виртуальной машине с 2 ядрами (Linux; на обычном ПК быстрее):

| | Время | Память процесса (своя) |
|---|---|---|
| Открыть AC-3 / E-AC-3 / DTS (декодирование + пики) | 39 / 43 / 73 с | 126–135 МБ |
| Рабочие файлы одной дорожки | 9,8 ГБ (float32 + пики) | |
| Волна: прокрутка (весь фильм / 10 мин / 10 с на экране), масштаб | 436 / 76 / 29 кадр/с, масштаб 99 кадр/с | |
| Спектрограмма: новый вид целиком (весь фильм / 10 мин / 10 с) | 1,5 / 2,7 / 0,4 с, потом 260–490 кадр/с | |
| Вырезать 30 мин / вставить / отменить / удалить с 200 метками | < 1 мс | |
| LUFS + LRA + true peak всего фильма | 50 с (182× реального времени) | |
| Нормализация до −23 LUFS (два прохода) | 99 с (91×) | 219 МБ |
| Лимитер с true peak / шумоподавление по всему фильму | 192 с (47×) / 361 с (25×) | 235 / 247 МБ |
| Сохранить FLAC 24 бит (4,5 ГБ, главы) / AC-3 640k | 64 с (141×) / 82 с (110×) | |
| Закрыть: рабочие файлы удалены, место вернулось | 4 с | |

«Своя» память — без страниц отображённых звуковых файлов (их Windows отдаёт по первому требованию, это кэш, а не память программы). Полный отчёт с RSS — в артефактах задачи `stress` в CI.
<!-- /stress -->

### Оформление
Как у ArtCore Convert: тёмная и светлая тема (или как в Windows), классический или современный стиль, иконки Font Awesome, интерфейс на русском и английском. Окно запоминает панели, режим вида, разделители, размеры диалогов и последние настройки. Автообновление через GitHub Releases.

## Горячие клавиши

<!-- hotkeys:ru -->
| Команда | Клавиши |
|---|---|
| **ArtCore Sound** | |
| Настройки | Ctrl+, |
| Горячие клавиши | F1 |
| Выход | Ctrl+Q |
| **Файл** | |
| Открыть | Ctrl+O |
| Сохранить | Ctrl+S |
| Сохранить как | Ctrl+Shift+S |
| Вернуть в видео | Ctrl+Shift+E |
| Audio CD → Рип Audio CD | Ctrl+Shift+D |
| Закрыть | Ctrl+W |
| Закрыть все | Ctrl+Shift+W |
| **Правка** | |
| Отменить | Ctrl+Z |
| Повторить | Ctrl+Y / Ctrl+Shift+Z |
| Вырезать | Ctrl+X |
| Копировать | Ctrl+C |
| Вставить | Ctrl+V |
| Вставить как новый файл | Ctrl+E |
| Удалить | Del |
| Обрезать по выделению | Ctrl+T |
| Заменить тишиной | Ctrl+L |
| Выделить всё | Ctrl+A |
| Снять выделение | Esc |
| Выделить до начала | Shift+Home |
| Выделить до конца | Shift+End |
| **Мультитрек** | |
| Новый мультитрек-проект | Ctrl+Alt+N |
| Отправить в мультитрек | Ctrl+Alt+T |
| Дорожки → Добавить стерео-дорожку | Ctrl+Shift+T |
| Дорожки → Добавить шину | Ctrl+Shift+B |
| Добавить файлы на дорожку | Ctrl+Shift+I |
| Разрезать клипы | S |
| Дублировать клипы | Ctrl+D |
| Открыть клип в редакторе | Ctrl+Shift+O |
| Привязка | N |
| Огибающие громкости и панорамы | E |
| Микшер | F9 |
| Punch-in/out по выделению | P |
| Свести в файл | Ctrl+Shift+X |
| **Метки** | |
| Добавить метку | M |
| Регион из выделения | R |
| Предыдущая метка | Ctrl+← |
| Следующая метка | Ctrl+→ |
| **Вид** | |
| Крупнее | ↑ / Ctrl+= |
| Мельче | ↓ / Ctrl+- |
| Весь файл | Ctrl+↑ |
| Выделение во весь экран | Ctrl+↓ |
| До сэмплов (1:1) | Ctrl+Shift+↑ |
| Вид документа → Волна | Alt+1 |
| Вид документа → Спектрограмма | Alt+2 |
| Вид документа → Волна + спектрограмма | Alt+3 |
| Панель спектра | F6 |
| В начало | Home |
| В конец | End |
| Назад | ← |
| Вперёд | → |
| **Реставрация** | |
| Спектральная правка | Ctrl+Shift+R |
| **Фильм** | |
| Окно видео | F8 |
| Автосинхронизация с эталоном | Ctrl+Shift+Y |
| **Инструменты** | |
| Статистика | Ctrl+I |
| Спектр | Ctrl+Shift+F |
| Спектрограмма вкл / выкл | F7 |
| Пакетная обработка | Ctrl+B |
| **Макросы** | |
| Записать макрос | Ctrl+Shift+M |
| **Воспроизведение** | |
| Воспроизвести | Пробел |
| Воспроизвести весь файл | Shift+Пробел |
| Пауза | Ctrl+Пробел |
| По кругу | Q |
| Запись | Ctrl+R |
| Громче | Ctrl+Alt+↑ |
| Тише | Ctrl+Alt+↓ |
| Без звука | Ctrl+Alt+M |
| **Мышь в окне волны** | |
| Выделить участок | Перетащить |
| Расширить выделение до точки | Shift+щелчок |
| Выделить весь файл | Двойной щелчок |
| Прокрутка по времени | Колесо |
| Масштаб у курсора мыши | Ctrl+колесо |
| Передвинуть метку или край региона | Перетащить флажок |
| Переименовать метку | Двойной щелчок по флажку |
| Перейти к месту файла | Щелчок по обзору вверху |
| Открыть файлы | Перетащить в окно |
<!-- /hotkeys:ru -->

## Скриншоты

| | |
|---|---|
| ![Эквалайзер](docs/screenshots/fx_eq_dark.png) | ![Спектрограмма](docs/screenshots/wave_spectrogram_dark.png) |
| ![Статистика](docs/screenshots/statistics_light.png) | ![Сохранение пакета](docs/screenshots/batch_output_light.png) |
| ![Горячие клавиши](docs/screenshots/hotkeys_dark.png) | ![Восстановление](docs/screenshots/recovery_dark.png) |
| ![Отчёт stress_long](docs/screenshots/stress_report_dark.png) | ![Спектральная правка](docs/screenshots/spectral_dark.png) |
| ![Удаление фона](docs/screenshots/fx_dehum_dark.png) | ![Клиппинг на волне](docs/screenshots/declip_marks_dark.png) |
| ![Плагин VST3](docs/screenshots/fx_vst3_dark.png) | ![Настройки VST3](docs/screenshots/settings_vst3_light.png) |
| ![Макрос](docs/screenshots/macro_dialog_dark.png) | ![Запись макроса](docs/screenshots/macro_recording_dark.png) |
| ![Рип Audio CD](docs/screenshots/cd_rip_dark.png) | ![Запись Audio CD](docs/screenshots/cd_burn_light.png) |

## Установка

Скачайте `ArtCoreSound-Setup-X.Y.Z.exe` в [Releases](https://github.com/Ruf790/artcore-sound/releases) — FFmpeg уже внутри. Есть переносная версия (`ArtCoreSound-portable.zip`: настройки в папке `Data` рядом с программой). Windows 10 / 11, 64 бит.

## Сборка

```
build.bat
```

Нужны Python 3.12 / 3.13 (64 бит) и Inno Setup 6.3+. Скрипт ставит зависимости, скачивает FFmpeg (BtbN, GPL), собирает exe (PyInstaller) и установщик. Тесты: `python -m pytest tests` (нужен ffmpeg в PATH). Скриншоты: `python tools/screenshots.py <папка>`. Проверка на длинном фильме: `python tools/stress_long.py <папка> --minutes 150 --dts`. Таблица клавиш для README: `python tools/hotkeys_md.py --readme`.

## English

An audio editor for Windows in the spirit of Sound Forge.

- **Files:** opens anything FFmpeg reads, sound from video included (a dialog picks the track); saves WAV 16 / 24 / 32 float (RF64 past 4 GB), FLAC, MP3, AAC, Opus and AC-3 with tags, markers and chapters, through a temporary file. «Open with» for every supported format.
- **Waveform and editing:** a lane per channel up to 7.1, zoom from the whole file to single samples (a column per device pixel at 125 / 150 %), an overview strip; cut, copy, paste, delete, silence, trim; unlimited undo kept on disk, with an undo history list. A redo branch that an edit throws away frees its files at once; short of disk space before a long job, the program forgets the redo steps itself and says so.
- **Processing and effects** with a live preview (loop, sliders heard on the fly, bypass, presets): volume, peak / true-peak and EBU R128 normalization with a true-peak ceiling, fades, reverse, invert, DC offset, trimming silence, sample rate and channels; a parametric and a graphic EQ, compressor, look-ahead limiter with a true-peak ceiling, delay, reverb, tempo and pitch (Rubber Band), noise reduction by a noise sample — the sample saved to and loaded from a file (`.acnoise`) — and click removal.
- **Analysis:** peak, RMS, LUFS and clipping in the status bar; Statistics per channel (BS.1770-4, matching ffmpeg's ebur128 within 0.1 LU); a spectrum panel and window; a spectrogram view.
- **Markers and regions** kept through edits and saved in WAV / FLAC / Opus / MP3 / M4A; film chapters open as regions. **Recording** from a WASAPI input; **playback** through WASAPI, 5.1 as 5.1.
- **Listening volume (2.0):** a speaker and a slider on the transport (mouse wheel 1 dB a step, a click on the speaker mutes), Ctrl+Alt+↑ / ↓ / M and the Transport menu. It changes only what you hear — playback, effect preview, input monitoring in the Record window («Listen to the input») — smoothly and on the fly; never the file, saving or the meters. Kept between runs.
- **Sound for films (new in 2.0):** File → Return to video (Ctrl+Shift+E) — the sound replaces a track of its film or is added, everything else copied without re-encoding (AC-3, E-AC-3, AAC, FLAC, Opus, PCM, DTS core; language, title, default, delay, chapters from markers; checked by ffprobe). A video window (F8, libmpv following the player's clock). Dub sync: shift, PAL 25 ↔ 23.976 / 24, two-point stretch, automatic sync to a reference with confidence. 5.1: channel matrix, dialogue boost, ATSC/ITU / LoRo / Pro Logic II downmix, upmix, channels to mono files and back.
- **Restoration (new in 2.0):** a Restoration menu — noise reduction by a sample and **adaptive** (no sample: minimum statistics, voice / music), clicks and **crackle**, **declipping** (clipping found and marked on the wave with the headroom it needs; LSAR, a spline for long runs), **hum removal** 50 / 60 Hz with harmonics (the frequency found and followed as it drifts), a **de-esser**; all with a preview, presets and batch steps. **Spectral editing** (Ctrl+Shift+R): select on the spectrogram (time, rectangle, brush, lasso), attenuate, erase, fill from around or boost; only the part touched is computed — outside the selection the sound is bit for bit the same.
- **VST3 plugins (new in 2.0)** through Spotify's pedalboard: Effects → VST3 plugins (by maker, the last used first), Settings → VST3 (folders, scan, those that did not load with Retry); the plugin's window has its parameters, **its own editor**, presets with its state and `.vstpreset` files, preview, bypass, the tail, its latency made up for; 5.1 all at once, **in pairs**, each channel alone or chosen channels only; a batch step. **A plugin that crashes or hangs does not take the program down:** each runs in a process of its own (`ArtCoreSound.exe --vst-host`, the sound through shared memory).
- **Macros (new in 2.0):** Macros → Record (Ctrl+Shift+M) keeps editing, processing, effects, plugins and saving with their settings; selections as seconds from the start or the end of the file, so one macro fits files of any length; a name, a hotkey, steps to reorder, switch off or change; run on a selection or a file as one undo step (the file untouched if a step fails); `.acmacro` import / export; a batch step.
- **Audio CD (new in 2.0):** rip (Ctrl+Shift+D) with CD-TEXT or **MusicBrainz** names into files of any format with tags and a name template with folders (16-bit FLAC / WAV bit for bit), into the editor per track or **the whole disc as one file with regions**; **secure reading** (each piece read twice, differing pieces re-read until two reads agree, a report with CRCs), the drive's read offset. Burn the regions, the markers or the whole file with chosen pauses and **CD-TEXT** (Cyrillic spelt in Latin letters), disc-at-once (the program builds the raw image) or track-at-once, erase a CD-RW, save as .cue / .bin — a rip burnt back unedited is the same disc bit for bit; through Windows' own IMAPI2.
- **Batch processing (new in 1.0)** — Tools → Batch processing (Ctrl+B): a folder (with subfolders, an extension filter) or a list of files (drag and drop), which track of a video (the default, by language, all); a chain of any Process and Effects steps — added, removed, reordered, switched on and off, set in the same effect windows with presets and a preview of the first 30 s of the first file; the output format and quality (or «as the source»), the same or another folder keeping the subfolders, a name template (`{name}`, `{n}`, `{n:3}`, `{date}`, `{track}`, `{lang}`, a suffix), skip / replace / new name for existing files, tags and markers kept; batches saved as presets (`.acbatch`). It runs in the background in worker processes (half the cores by default), streaming through the disk with the disk space reserved per file; per-file and overall progress, pause, stop (unfinished files removed); a broken file does not stop the batch; a log per file (done / error in plain words, LUFS and peak before and after, time), a summary, a CSV or text report.
- **Robustness (new in 1.0):** a journal of every document's piece table, undo history and markers — after a crash the next start offers the unsaved documents back with their history; unhandled errors are logged (with the last actions) and shown with «Copy the report»; a full disk mid-operation gives a clear message, leaves the document as it was and removes the partial files; errors in plain words; long paths and Cyrillic names.
- **Long files:** a 2–3 h 5.1 film opens, draws, edits and processes from disk without memory growth — see `tools/stress_long.py` (a CI job run by hand).
- Dark and light themes, a classic and a modern look, Russian and English; the window remembers its panels, view, splitters, dialog sizes and last settings; automatic updates through GitHub Releases.

### Hotkeys

<!-- hotkeys:en -->
| Command | Keys |
|---|---|
| **ArtCore Sound** | |
| Settings | Ctrl+, |
| Hotkeys | F1 |
| Quit | Ctrl+Q |
| **File** | |
| Open | Ctrl+O |
| Save | Ctrl+S |
| Save as | Ctrl+Shift+S |
| Return to video | Ctrl+Shift+E |
| Audio CD → Rip an Audio CD | Ctrl+Shift+D |
| Close | Ctrl+W |
| Close all | Ctrl+Shift+W |
| **Edit** | |
| Undo | Ctrl+Z |
| Redo | Ctrl+Y / Ctrl+Shift+Z |
| Cut | Ctrl+X |
| Copy | Ctrl+C |
| Paste | Ctrl+V |
| Paste as a new file | Ctrl+E |
| Delete | Del |
| Trim to the selection | Ctrl+T |
| Silence | Ctrl+L |
| Select all | Ctrl+A |
| Select none | Esc |
| Select to the start | Shift+Home |
| Select to the end | Shift+End |
| **Multitrack** | |
| New multitrack project | Ctrl+Alt+N |
| Send to multitrack | Ctrl+Alt+T |
| Tracks → Add a stereo track | Ctrl+Shift+T |
| Tracks → Add a bus | Ctrl+Shift+B |
| Add files to the track | Ctrl+Shift+I |
| Split the clips | S |
| Duplicate the clips | Ctrl+D |
| Open the clip in the editor | Ctrl+Shift+O |
| Snap | N |
| Volume and pan envelopes | E |
| Mixer | F9 |
| Punch in / out on the selection | P |
| Mix down to a file | Ctrl+Shift+X |
| **Markers** | |
| Add a marker | M |
| A region of the selection | R |
| Previous marker | Ctrl+← |
| Next marker | Ctrl+→ |
| **View** | |
| Zoom in | ↑ / Ctrl+= |
| Zoom out | ↓ / Ctrl+- |
| Whole file | Ctrl+↑ |
| Zoom to the selection | Ctrl+↓ |
| Down to samples (1:1) | Ctrl+Shift+↑ |
| Document view → Waveform | Alt+1 |
| Document view → Spectrogram | Alt+2 |
| Document view → Waveform + spectrogram | Alt+3 |
| Spectrum panel | F6 |
| Go to the start | Home |
| Go to the end | End |
| Rewind | ← |
| Forward | → |
| **Restoration** | |
| Spectral editing | Ctrl+Shift+R |
| **Film** | |
| Video window | F8 |
| Auto sync to a reference | Ctrl+Shift+Y |
| **Tools** | |
| Statistics | Ctrl+I |
| Spectrum | Ctrl+Shift+F |
| Spectrogram on / off | F7 |
| Batch processing | Ctrl+B |
| **Macros** | |
| Record a macro | Ctrl+Shift+M |
| **Transport** | |
| Play | Space |
| Play the whole file | Shift+Space |
| Pause | Ctrl+Space |
| Loop | Q |
| Record | Ctrl+R |
| Louder | Ctrl+Alt+↑ |
| Quieter | Ctrl+Alt+↓ |
| Mute | Ctrl+Alt+M |
| **The mouse in the waveform** | |
| Select a stretch | Drag |
| Extend the selection to a point | Shift+click |
| Select the whole file | Double-click |
| Scroll in time | Wheel |
| Zoom about the mouse | Ctrl+wheel |
| Move a marker or a region's edge | Drag its flag |
| Rename a marker | Double-click its flag |
| Go to a place of the file | Click the overview at the top |
| Open files | Drop them on the window |
<!-- /hotkeys:en -->

License: GNU GPL v3. Third-party components: [THIRD-PARTY.md](THIRD-PARTY.md).
