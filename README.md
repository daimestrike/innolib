# Библиотека инноваций

Реестр инновационных проектов и идей: воронка стадий, мастер из 4 вопросов с разбором ответов через LLM, подсказка похожих проектов, выгрузка в Excel.

**Работает в закрытом контуре без интернета.**
- Сервер использует только стандартную библиотеку Python 3.9+, `pip install` не нужен.
- База — один файл SQLite.
- Шрифты лежат в `static/fonts`, внешних CDN нет.
- LLM необязателен. Без него мастер работает, а карточка заполняется из ответов как есть.

## Скачать

Готовые архивы лежат в [Releases](https://github.com/daimestrike/innolib/releases):
- `innolib-<версия>.tar.gz` — исходники, запуск через `python3 app.py` или сборка образа в контуре;
- `innolib-image-<версия>.tar.gz` — готовый Docker-образ (`docker load -i …`);
- `SHA256SUMS.txt` — контрольные суммы для проверки после переноса.

Текущая версия — в файле `VERSION`. Её также показывают `/healthz`, `/api/config` и подвал интерфейса.

## Состав

```
app.py                  сервер + CLI (import / export)
static/                 интерфейс: index.html, app.js, app.css, шрифты
data/seed_demo.json     16 демо-кейсов (грузятся в пустую базу, если SEED_DEMO=1)
data/import_template.csv  шаблон для загрузки реальных проектов
Dockerfile, docker-compose.yml, .env.example
deploy/innolib.service  запуск через systemd без Docker
deploy/nginx.conf.example  публикация под префиксом /innolib/
build_offline.sh        сборка архива и Docker-образа для переноса в контур
```

## Быстрый старт

```bash
python3 app.py
# открыть http://<сервер>:8080
```

## Развёртывание в контуре

### Вариант 1. Docker, готовый образ из релиза

Скачать из Releases оба архива и перенести в контур:
```bash
sha256sum -c SHA256SUMS.txt
docker load -i innolib-image-1.0.0.tar.gz
tar xzf innolib-1.0.0.tar.gz && cd innolib-1.0.0
cp .env.example .env          # заполнить при необходимости
INNOLIB_VERSION=1.0.0 docker compose up -d   # образ уже загружен, сборка не нужна
```
Собрать то же самое локально: `./build_offline.sh` кладёт архивы в `dist/`.

### Вариант 2. Docker, сборка внутри контура

Нужен только базовый образ Python из внутреннего реестра:
```bash
docker build --build-arg BASE_IMAGE=<внутренний-реестр>/python:3.12-slim --build-arg VERSION=$(cat VERSION) -t innolib:latest .
docker compose up -d
```

### Вариант 3. Без Docker (systemd)

Достаточно `python3` на сервере. Команды описаны в шапке `deploy/innolib.service`.

База хранится в томе `innolib-data` (Docker) или в `data/innolib.sqlite3` (systemd). Резервная копия — это просто копия файла, либо `GET /api/export.json`.

## Настройки (.env)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `PORT` | 8080 | порт |
| `DB_PATH` | data/innolib.sqlite3 | файл базы |
| `SEED_DEMO` | 1 | загрузить демо-кейсы в пустую базу |
| `ADMIN_TOKEN` | пусто | включает `DELETE /api/cases/<id>` с заголовком `X-Admin-Token` |
| `LLM_URL` | пусто | OpenAI-совместимый endpoint до `/v1` включительно |
| `LLM_MODEL` | пусто | имя модели |
| `LLM_API_KEY` | пусто | ключ, если нужен |
| `LLM_TIMEOUT` | 60 | таймаут запроса к LLM, сек |
| `LLM_CA_BUNDLE` / `LLM_VERIFY_TLS` | — / 1 | внутренний сертификат для HTTPS |

LLM подключается к любому серверу с API `/v1/chat/completions`: vLLM, Ollama, LiteLLM, TGI. Если внутренняя модель (например, GigaChat) использует свой протокол, поставьте перед ней LiteLLM-прокси. Запросы к LLM идут напрямую, без системного прокси.

## Загрузка реальных проектов

1. Заполните `data/import_template.csv` в Excel и сохраните как CSV UTF-8. Разделитель `;` или `,`.
   - Стадии можно писать по-русски: идея, проработка, пилот, внедрено, остановлено.
   - Тип: проект или идея. Эффект: план или факт. Теги перечисляются через `;`.
2. Загрузите файл:
```bash
python3 app.py import cases.csv
# в Docker:
docker compose exec innolib python3 app.py import /data/cases.csv
```
Для Docker файл нужно сначала положить в том `/data`, например через `docker cp`.

Чтобы не загружать демо-кейсы: задайте `SEED_DEMO=0` до первого запуска, либо удалите базу и запустите заново.

## API

| Метод | Путь | |
|---|---|---|
| GET | `/api/cases` | все кейсы |
| POST | `/api/cases` | создать (код INN-xxx назначается сервером) |
| PATCH | `/api/cases/<id>` | изменить поля |
| DELETE | `/api/cases/<id>` | удалить (нужен `ADMIN_TOKEN`) |
| POST | `/api/parse` | разбор ответов мастера через LLM |
| GET | `/api/export.csv`, `/api/export.json` | выгрузка |
| GET | `/healthz` | проверка живости |

## Выпуск новой версии

1. Поднять номер в `VERSION` по SemVer: исправление → PATCH, новая функция → MINOR, несовместимое изменение → MAJOR.
2. Добавить раздел в `CHANGELOG.md`.
3. Закоммитить и запушить в `main`.

GitHub Actions увидит новую версию в `VERSION`, поставит тег `vX.Y.Z`, соберёт архив и Docker-образ и опубликует релиз с заметками из CHANGELOG. Запустить выпуск вручную можно так: Actions → Release → Run workflow.

## Обновление в контуре

База хранится отдельно от кода (том `innolib-data` или `data/`), поэтому обновление не трогает кейсы:
```bash
docker load -i innolib-image-<новая>.tar.gz
INNOLIB_VERSION=<новая> docker compose up -d
```
Перед обновлением стоит сохранить резервную копию: `GET /api/export.json`.

## Ограничения прототипа

- Нет авторизации: доступ ограничивается сетью или reverse-proxy с SSO.
- Вики не читается автоматически. Текст страницы вставляется в мастер вручную. Интеграцию с API вики (Confluence и т.п.) можно добавить в `app.py`.
- Похожие проекты считаются по ключевым словам в браузере. Для большого реестра можно перейти на эмбеддинги из той же LLM.
