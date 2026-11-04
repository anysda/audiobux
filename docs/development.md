# Разработка

## Структура пакета

Пакет `audiobux` - один слой модулей и подпакет `formats/` с форматами книг. По ролям:

| Модуль | Что делает |
|---|---|
| `__main__.py` | командная строка: `audiobux worker`, `audiobux web`, `audiobux password`, `audiobux openapi` и `audiobux book` (одна книга без очереди) |
| `config.py` | настройки из переменных окружения `AUDIOBUX_*` |
| `book.py` | рабочая папка книги: исходник, выходы шагов, отметки о готовности |
| `pipeline.py` | шаги книги по порядку (`STEPS`) и их запуск |
| `worker.py` | процесс worker: книги из очереди по одной, выгрузка моделей, ошибки, сокет |
| `library.py` | библиотека и очередь в `audiobux.db` (SQLite, WAL), файлы книг в `library/<id>/` |
| `contract.py` | сокет worker: закрытый список методов, проверка запроса, ответы; его импортирует и web |
| `web.py` | процесс web: API интерфейса на FastAPI, в worker ходит только через `contract` |
| `listening.py` | своя база web: где слушатель в каждой книге и прослушана ли она |
| `auth.py` | вход в web на fastapi-users: один пользователь, хэш пароля и сессии в `web/auth.db` |
| `quiet.py` | тихие часы: внутри окна книга не начинается, начатая встаёт между кусками |
| `formats/` | форматы на входе: список с сигнатурами (`__init__.py`), `Document`, по модулю на формат |
| `formats/pages.py` | страницы текста в главы: PDF и DjVu со слоем текста, страницы после OCR |
| `text.py` | общие для извлечения и правки шаблоны: концы предложений, подписи, метки рисунков, код |
| `figures.py` | рисунки и таблицы: метки в тексте, вырезка, описания словами |
| `cleanup.py` | правка текста правилами и языковой моделью |
| `speech.py` | текст для голоса: числа и знаки словами, капс, латиница, ударения, паузы |
| `chunk.py` | главы на куски, которые голос читает за один проход |
| `takes.py` | проверка озвученного куска и расстановка пауз |
| `models.py` | гнёзда моделей (OCR, TTS, распознавание) и их загрузка |
| `fetch.py` | загрузка всех нужных настройкам моделей в контейнере fetch (worker ждёт её и передаёт ход), закреплённых по ревизии и sha256 файлов, с ходом в байтах и повтором после сбоя сети |
| `runtime.py` | то, на чём работают модели (torch с CUDA, F5, faster-whisper, RUAccent, llama-server): ставится worker при первом запуске в `<data>/runtime/<хеш>` по `pylock.runtime.toml`, файлы сверяются по sha256 |
| `f5.py` | голос F5-TTS, ставится с рантаймом (см. «Окружение») |
| `llm.py` | локальная языковая модель за сервером llama.cpp |
| `m4b.py` | сборка m4b: главы, обложка, ровная громкость |
| `reading.py` | текст для читателя блоками: заголовки, абзацы, листинги, рисунки и абзац, который их озвучивает |
| `epub.py` | EPUB рядом с аудиокнигой |
| `sync.py` | `sync.json`: текст для читателя со временем в m4b, по выравниванию слов |
| `refresh.py` | готовая книга прежнего разбора: название, автор и главы заново, звук прежний |
| `cover.py`, `webcover.py` | обложка из книги или из сети через `coverfinder/` |

Границы импортов проверяет `tests/test_imports.py`:

- циклов нет;
- `models`, `fetch` и всё, что они тянут, не знают о форматах книг, worker, библиотеке,
  контракте, web и конвейере. Если новому модулю гнёзд моделей нужен, например, `formats`, значит,
  код лежит не там;
- `formats` не знает о моделях и шагах после себя: `models`, `llm`, `f5`, `takes`,
  `speech`, `chunk`, `cleanup`, `figures`, `pipeline`, `worker`. Общее с правкой
  текста лежит в `text.py`;
- список форматов (`formats/__init__.py` и то, что он тянет) и `contract` берут
  только стандартную библиотеку: их импортирует и лёгкий веб-процесс;
- `web` дотягивается из пакета только до `contract`, `config`, списка форматов
  и своих баз `listening` и `auth`, а `worker` не знает о `web`.

## Окружение

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

Для тестов нужны `ffmpeg` и `djvulibre` (`djvulibre-bin` в Debian и Ubuntu).
F5 и torch для тестов не нужны: голос в тестах - тон или тишина из `models.ToneTts`.

Зависимости разложены по группам в `pyproject.toml`: `web` для процесса web, `worker`
для запуска worker, `models` и `f5` для моделей. Образы ставятся по файлам с точными
версиями и sha256: `pylock.web.toml`, `pylock.worker.toml` и `pylock.runtime.toml`.
Как их пересобрать, написано в `pyproject.toml` над группой `runtime`.

Тяжёлое (torch с CUDA, F5, faster-whisper, RUAccent, llama-server) в образ worker не
входит: его вместе с моделями ставит контейнер fetch (`audiobux fetch`) в
`<data>/runtime/<хеш lock-файла>` (`runtime.py`) в фоне, install.sh его не ждёт;
worker выхода в сеть не имеет и берёт поставленное. Новый lock-файл ставится рядом, старая папка удаляется
после успеха. У `f5-tts` нет его собственных зависимостей: они нужны только обучению
и демо (gradio, wandb, datasets, accelerate). Строку, которой его пакет `model`
импортирует тренер, `runtime.py` убирает при распаковке. Локально голос F5 ставится так:

```sh
uv pip install -r pylock.runtime.toml
uv pip install --no-deps encodec==0.1.1
sed -i '/^from f5_tts.model.trainer import Trainer$/d' \
  "$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/f5_tts/model/__init__.py"
```

## Требования

| Что | Сколько |
|---|---|
| Образы | web 92 МБ, worker 468 МБ |
| Рантайм моделей в `runtime/` | 6,3 ГБ на диске, загрузка 3,86 ГБ |
| Модели по умолчанию | 28,05 ГБ |
| Данные на час звука | около 31 МБ |
| Видеопамять | не измерено |
| Оперативная память | не измерено |

В `models/` могут лежать и другие файлы: transformers при первой загрузке дописывает
вторую копию весов выравнивателя в safetensors (1,26 ГБ).

Итого на машине под образы, рантайм и модели нужно около 36 ГБ свободного места, плюс около 31 МБ
на каждый час готовых книг.

## Образы

Образы собирает GitHub Actions (`.github/workflows/images.yml`) на каждый пуш в `main` и
тега `v*` и кладёт в `ghcr.io/anysda/audiobux/worker`, `.../web` и `.../cover`. Теги: `v<версия>` -
выпуск, его ставит `install.sh`; `latest` - последний `main`; `sha-<коммит>` - каждый коммит.
Образ, который не импортирует свой процесс, в реестр не уходит.

На тег выпуска CI кладёт в выпуск на GitHub `images.txt`: строка `<образ> sha256:<digest>` на
каждый образ. `install.sh` ставит последний выпуск (куда ведёт `github.com/anysda/audiobux/releases/latest`,
каталог `.git` ему не нужен): тянет образы по тегу выпуска, сверяет их `RepoDigests` с `images.txt` и пишет digest в `.env`
(`AUDIOBUX_*_DIGEST`); `compose.yaml` запускает образы только по ним. Другой образ под тем же
тегом, из реестра или поставленный руками, установку останавливает.

Собрать образы у себя и запустить их вместо образов выпуска (`compose.yaml` и тогда читает digest
из `.env`, так что сначала `./install.sh`):

```sh
docker build --target web -t audiobux/web:local .
docker build --target worker -t audiobux/worker:local .
docker build -t audiobux/cover:local coverfinder
printf 'services:\n' >compose.override.yaml
for s in worker web cover; do printf '  %s:\n    image: audiobux/%s:local\n' $s $s >>compose.override.yaml; done
docker compose up -d
```

`compose.override.yaml` compose читает сам, в git он не попадает. Вернуться к образам выпуска:
удалить его и запустить `./install.sh`.

## Стиль и проверки

Все настройки в `pyproject.toml`. Перед коммитом их гоняет pre-commit, в GitHub Actions
те же проверки идут заданиями `ruff`, `mypy`, `vulture`, `imports` и `pytest`
(`.github/workflows/checks.yml`) на каждый пуш в `main` и pull request.
Вручную из корня репозитория:

```sh
ruff check                 # правила стиля и ошибок
ruff format                # оформление; в CI - ruff format --check
mypy                       # строгая проверка типов пакета
vulture                    # мёртвый код пакета
pytest tests/test_imports.py --no-cov   # только границы импортов
pytest                     # все тесты с покрытием
```

Описание API в `docs/openapi.json` пишется из приложения, тест сверяет его с ним.
После правки ручек или схем:

```sh
audiobux openapi > docs/openapi.json
```

Правила:

- Строка до 120 знаков, оформление за `ruff format`, спорить с ним не нужно.
- Каждая функция пакета с типами, `mypy --strict` без замечаний. Библиотеки без типов
  перечислены в `[tool.mypy.overrides]`; `# type: ignore` в коде не ставим, а
  чиним тип или дописываем библиотеку туда.
- Мёртвого кода нет: vulture смотрит только пакет, поэтому имя, которое нужно
  одним тестам, для него мёртвое. Ложные находки (атрибут, который читает
  библиотека) идут в `ignore_names` с объяснением.
- Кириллица и типографские кавычки в строках нормальны, правила RUF001-003
  для них выключены.
- Докстроки и комментарии по-английски, коротко: что и почему, без пересказа кода.
  Подсказки модели и сообщения для слушателя по-русски.

## Фронт

Интерфейс в `frontend/`: Svelte 5 и TypeScript, сборка Vite. `Dockerfile` собирает его
отдельной стадией на `node:22.22.3-slim`, а в образ web кладёт только `dist/`
как `/app/static`, последним слоем, поэтому правка фронта не пересобирает Python.
Веб-процесс отдаёт файлы из `AUDIOBUX_FRONTEND`: в образе web это `/app/static`, при запуске
из репозитория по умолчанию `frontend/dist`.

| Файл | Что делает |
|---|---|
| `src/App.svelte` | раскладка по ширине: до 1000 px телефон, от 1000 px ПК; на `/login` - вход |
| `src/Login.svelte` | страница входа: логин и пароль |
| `src/Phone.svelte`, `src/Pc.svelte` | экраны двух раскладок: библиотека, книга, плеер |
| `src/lib/state.svelte.ts` | состояние на всё приложение: книги, отметки, выбор, один `<audio>` |
| `src/lib/api.ts` | ручки API, которые читает интерфейс, переписаны из `docs/openapi.json` руками |
| `src/lib/Reader.svelte` | вкладка «Книга»: текст из `sync.json`, подсветка услышанного |
| `src/lib/*.svelte`, `*.ts` | обложка, заголовок с подбором кегля, ползунок, переключатели, движение |
| `src/fonts/` | шрифты с лицензией OFL, урезанные до кириллицы и латиницы |

После `npm run build` страницу отдаёт сам `audiobux web`. Для правок с горячей
перезагрузкой есть `npm run dev`: Vite проксирует `/api` к `audiobux web` на `127.0.0.1:8080`.
Без `--tls` `audiobux web` отвечает по HTTP; пароль входа задаёт
`echo 'пароль' | audiobux password`, без входа - `AUDIOBUX_LOGIN=off`.

```sh
npm --prefix frontend ci
npm --prefix frontend run dev       # http://localhost:5173
npm --prefix frontend run check     # svelte-check, предупреждения считаются ошибками
npm --prefix frontend run lint      # eslint
npm --prefix frontend run build     # frontend/dist
```

В GitHub Actions эти проверки, `npm test` (vitest) и `npm ci` идут заданием `frontend`.

Правила:

- Сторонних запросов нет: шрифты лежат в `src/fonts`, ни CDN, ни Google Fonts.
- Веб-процесс отдаёт страницу с CSP `default-src 'self'`. Поэтому в разметке нет
  скриптов с телом, атрибутов `on*=`, `<style>` и статических `style="..."`. Переменное
  значение ставится через `style:` или `el.style`, а постоянное живёт в `app.css`.
  Картинок и шрифтов в `data:` нет: всё файлами (`assetsInlineLimit: 0`).
- Переносов по слогам нет, `hyphens` не включаем. Длинный заголовок ужимает
  кегль (`lib/fit.ts`), а слово переносится только целиком.
- Интерфейс ничего не хранит в браузере: позиция и отметки живут на сервере,
  вид читалки сбрасывается при перезагрузке.
- Текст для слушателя по-русски, книга отмечается словом «Прослушано».

## Как добавить шаг конвейера

Шаг - функция `(book: Book, ctx: Context) -> None` в `pipeline.py` и строка в `STEPS`.

1. Функция читает выход предыдущего шага (`book.read("fixed.json")`) и пишет свой
   (`book.write("normalized.json", data)`). Отметку о готовности ставит `run()`
   после того, как шаг вернулся, так что упавший шаг при перезапуске пойдёт заново.
2. Долгий шаг делится на части, каждая сразу ложится на диск, а перед частью
   стоит `ctx.quiet.wait()`: так шаг переживает перезапуск, тихие часы и удаление книги.
3. Модель шаг берёт из `ctx.models`. Языковую модель после шага отпускает
   `ctx.models.release_llm()` в `finally`, иначе она держит видеокарту.
4. Новый шаг встаёт в `STEPS` на своё место; имя шага - это и имя отметки, и
   значение для `audiobux book --until`.
5. Новая настройка - поле в `Settings` и строка в `from_env` с переменной
   `AUDIOBUX_<ИМЯ>`; она идёт в таблицу настроек в `docs/architecture.md`, а если её задают через `.env` - ещё и в README.

## Как добавить формат

Формат - это модуль `audiobux/formats/<имя>.py` с функцией
`read(path, out, depth) -> Document`, строка в `KINDS` и тест. Остальной код не
трогается: конвейер получает `Document` и не знает, из какого файла он пришёл, а
список расширений для выбора файла и проверки загрузки берётся из того же `KINDS`.

Формат узнаётся по содержимому файла (`formats.detect`), не по расширению: файл
с чужой сигнатурой не принимается, как бы он ни назывался. Новый формат - только
своим маленьким разбором на том, что уже стоит (`lxml`, `zipfile`, `pymupdf`), без
конвертеров.

Пример по шагам: выдуманный формат «сказки» - текстовый файл, который начинается
строкой `TALE`, глава открывается строкой `# Название`. Тот же разбор тест
`test_made_up_format_goes_all_the_way_to_m4b` подставляет в `KINDS` и проводит
книгу по конвейеру до m4b, не трогая пакет.

1. Файл `audiobux/formats/tale.py`:

   ```python
   """Tales: a `TALE` line, then `# Title` to open a chapter and a paragraph a line."""

   from pathlib import Path

   from .document import Document


   def read(path: Path, out: Path, depth: int) -> Document:
       book = Document(title="Сказки", author="Народ")
       for line in path.read_text(encoding="utf-8").splitlines()[1:]:
           if line.startswith("# "):
               book.chapters.append({"title": line[2:], "paragraphs": [line[2:]]})
           elif line.strip():
               book.chapters[-1]["paragraphs"].append(line)
       return book
   ```

   Что заполнять в `Document` (`formats/document.py`):

   - `chapters` - главы с абзацами для книги с текстом; для скана пусто, а в
     `pages` лежат имена картинок страниц в `out` (`pages/0001.png`), их читает OCR;
   - `figures` - рисунки: картинку кладите в `out/figures/`, в текст на её место
     ставьте абзац `text.mark(n)`, где `n` - номер в `figures`;
   - `outline` - оглавление `(уровень, название, номер страницы)`; главы по нему
     берите до глубины `depth` (`pages.top_levels`), для страниц текста есть
     `pages.from_pages`;
   - `cover` и `first_page` - JPEG обложки и первой страницы, если они есть;
     `title` пустой, если файл его не знает: тогда книга идёт «Без названия»;
   - `keep_source` - исходник сам годится в читалку и выдаётся как есть (так у EPUB).

   Модуль формата не импортирует модели, языковую модель и шаги конвейера, это
   проверяет `tests/test_imports.py`. Тяжёлые библиотеки - только в нём, не в
   `formats/__init__.py`.

2. Строка в `KINDS` в `audiobux/formats/__init__.py`. Имя совпадает с именем
   модуля. Сигнатура - байты по смещению `offset` от начала файла; с `within`
   они ищутся где угодно в первых `within` байтах (так у PDF), а `check(path)`
   заглядывает в файл, когда байт мало (EPUB - zip с `META-INF/container.xml`).
   Читаются первые `HEAD` = 1024 байта, `check` пишется на стандартной библиотеке:

   ```python
   KINDS = [
       Kind("pdf", (".pdf",), b"%PDF-", within=1024),
       Kind("djvu", (".djvu",), b"AT&TFORM"),
       Kind("epub", (".epub",), b"PK\x03\x04", check=is_epub),
       Kind("tale", (".tale",), b"TALE\n"),
   ]
   ```

3. Тест в `tests/test_formats.py`: книга нового формата собирается в фикстуре из
   `tests/data/chekhov.txt` (как `chekhov_pdf` в `conftest.py`), проверяются главы
   из `formats.read`, сигнатура в `test_every_format_is_told_by_its_signature`
   (добавьте образец в фикстуру `samples`) и проход конвейера:

   ```python
   def test_tale_goes_all_the_way_to_m4b(tmp_path, chekhov_tale):
       book = new_book(tmp_path, chekhov_tale)
       info = probe(pipeline.run(book, context(tmp_path)))
       assert [c["tags"]["title"] for c in info["chapters"]] == TITLES
   ```

   `test_extensions_come_from_the_list` перечисляет расширения: новое дописывается
   туда же.

4. Новая зависимость для разбора - в `pyproject.toml` с причиной в описании
   изменения; если у неё нет типов, она идёт в `[tool.mypy.overrides]`.

## Как писать тесты

- Тест проверяет поведение, которое видно снаружи: файл на выходе, главы, текст,
  длительность. Внутренние функции проверяем, когда в них сидит правило
  (`cleanup`, `speech`, `chunk`).
- Книги для тестов собираются в `tests/conftest.py` из `tests/data/chekhov.txt`
  (фикстуры `chekhov_epub`, `chekhov_pdf`, `chekhov_scan`, `chekhov_djvu`), чужие
  файлы в репозиторий не кладём.
- Конвейер целиком: `context(tmp_path)` и `new_book(tmp_path, source)` из
  `tests/test_pipeline.py` дают настройки без моделей и рабочую папку. Модель
  подменяется своим классом с нужным методом, например наследником
  `models.ToneTts` или языковой моделью с готовыми ответами.
- Сеть и видеокарта в тестах не нужны. Тест, которому нужен настоящий файл
  книги, берёт его из `AUDIOBUX_TEST_BOOK` и пропускается без неё.
- Исправленная ошибка приходит с тестом, который без исправления падает.

## Коммиты

Одна строка по-русски: что изменилось для книги или для того, кто её слушает,
без префиксов, точки в конце и тела. Примеры из истории:

```
Подпись к рисунку посреди предложения не рвёт абзац
F5 не глотает последнее слово коротких кусков
EPUB рядом с аудиокнигой
```

Коммит - одно законченное изменение, проверки на нём зелёные.
