#!/usr/bin/env python3
"""Библиотека инноваций — сервер реестра кейсов.

Только стандартная библиотека Python 3.9+: никаких pip install,
работает в закрытом контуре без доступа в интернет.

Запуск:
    python3 app.py                      # веб-сервер
    python3 app.py import cases.csv     # загрузить кейсы из CSV/JSON
    python3 app.py export > backup.json # выгрузить реестр

Настройки — через переменные окружения (см. .env.example).
"""
import csv
import io
import json
import mimetypes
import os
import re
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
try:
    VERSION = (BASE_DIR / "VERSION").read_text().strip()
except OSError:
    VERSION = "dev"
STATIC_DIR = BASE_DIR / "static"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "data" / "innolib.sqlite3"))
SEED_DEMO = os.environ.get("SEED_DEMO", "1") == "1"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

LLM_URL = os.environ.get("LLM_URL", "").rstrip("/")  # напр. http://llm.local:8000/v1
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))
LLM_CA_BUNDLE = os.environ.get("LLM_CA_BUNDLE", "")
LLM_VERIFY_TLS = os.environ.get("LLM_VERIFY_TLS", "1") == "1"

MAX_BODY = 1_000_000

STAGES = ["idea", "research", "pilot", "launched", "stopped"]
STAGE_ALIASES = {
    "идея": "idea", "проработка": "research", "исследование": "research",
    "пилот": "pilot", "внедрено": "launched", "запущено": "launched",
    "тираж": "launched", "остановлено": "stopped", "отложено": "stopped",
}
DIRECTIONS = ["GenAI", "Компьютерное зрение", "OCR/VLM", "Антифрод",
              "Аналитика и прогнозы", "Автоматизация процессов"]
FIELDS = ["title", "type", "stage", "direction", "problem", "solution", "effect",
          "effectKind", "unit", "owner", "tags", "wiki"]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"[{now_iso()}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS cases(
                id TEXT PRIMARY KEY, code TEXT UNIQUE, data TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            self.conn.commit()

    def count(self):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]

    def _next_code(self):
        rows = self.conn.execute("SELECT code FROM cases").fetchall()
        n = max([int(re.sub(r"\D", "", r[0]) or 0) for r in rows] or [0])
        return f"INN-{n + 1:03d}"

    @staticmethod
    def _row(r):
        d = json.loads(r["data"])
        d.update(id=r["id"], code=r["code"], createdAt=r["created_at"], updatedAt=r["updated_at"])
        return d

    def list(self):
        with self.lock:
            rows = self.conn.execute("SELECT * FROM cases ORDER BY updated_at DESC").fetchall()
        return [self._row(r) for r in rows]

    def get(self, cid):
        with self.lock:
            r = self.conn.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone()
        return self._row(r) if r else None

    def create(self, data, created_at=None, updated_at=None):
        doc = clean_case(data)
        with self.lock:
            cid = uuid.uuid4().hex[:12]
            code = self._next_code()
            c = created_at or now_iso()
            self.conn.execute("INSERT INTO cases VALUES(?,?,?,?,?)",
                              (cid, code, json.dumps(doc, ensure_ascii=False), c, updated_at or c))
            self.conn.commit()
        return self.get(cid)

    def update(self, cid, patch):
        cur = self.get(cid)
        if not cur:
            return None
        merged = {k: cur.get(k) for k in FIELDS}
        merged.update({k: v for k, v in patch.items() if k in FIELDS})
        doc = clean_case(merged)
        with self.lock:
            self.conn.execute("UPDATE cases SET data=?, updated_at=? WHERE id=?",
                              (json.dumps(doc, ensure_ascii=False), now_iso(), cid))
            self.conn.commit()
        return self.get(cid)

    def delete(self, cid):
        with self.lock:
            n = self.conn.execute("DELETE FROM cases WHERE id=?", (cid,)).rowcount
            self.conn.commit()
        return n > 0


def s(v, limit=4000):
    return str(v if v is not None else "").strip()[:limit]


def clean_case(d):
    stage = s(d.get("stage")).lower()
    stage = STAGE_ALIASES.get(stage, stage)
    tags = d.get("tags") or []
    if isinstance(tags, str):
        tags = re.split(r"[;,]", tags)
    typ = s(d.get("type")).lower()
    return {
        "title": s(d.get("title"), 200) or "Без названия",
        "type": "idea" if typ in ("idea", "идея") else "project",
        "stage": stage if stage in STAGES else "idea",
        "direction": s(d.get("direction"), 80) or DIRECTIONS[-1],
        "problem": s(d.get("problem")),
        "solution": s(d.get("solution")),
        "effect": s(d.get("effect"), 500),
        "effectKind": "fact" if s(d.get("effectKind")).lower() in ("fact", "факт") else "plan",
        "unit": s(d.get("unit"), 200),
        "owner": s(d.get("owner"), 200),
        "tags": [s(t, 40) for t in tags if s(t)][:8],
        "wiki": s(d.get("wiki"), 500),
    }


# ---------------------------------------------------------------- LLM
def llm_enabled():
    return bool(LLM_URL and LLM_MODEL)


def build_prompt(p):
    parts = []
    if p.get("wikiText"):
        parts.append("ТЕКСТ СТРАНИЦЫ ВИКИ:\n" + s(p["wikiText"], 12000) + "\n")
    labels = [("what", "Что сделали / хотят сделать"), ("problem", "Проблема и для кого"),
              ("stage", "Стадия (выбрана сотрудником)"), ("effect", "Эффект"),
              ("followup", "Уточнения")]
    for k, lbl in labels:
        if p.get(k):
            parts.append(f"{lbl}: {s(p[k], 3000)}")
    followup = ('"один короткий уточняющий вопрос, если эффект или масштаб совсем неясны, иначе null"'
                if p.get("allowFollowup") else "null")
    return (
        "Ты помогаешь вести реестр инновационных проектов и идей розничной сети X5. "
        "Ниже сведения от сотрудника. Разложи их по полям карточки.\n\n"
        + "\n".join(parts) +
        "\n\nВерни ТОЛЬКО JSON без пояснений такого вида:\n"
        '{"title":"краткое название до 60 символов",'
        '"type":"project или idea (idea — если ещё ничего не сделано)",'
        '"stage":"idea | research | pilot | launched | stopped",'
        f'"direction":"одно из: {" | ".join(DIRECTIONS)}",'
        '"problem":"1–2 предложения","solution":"1–2 предложения",'
        '"effect":"эффект кратко, с цифрами если есть, иначе пустая строка",'
        '"effectKind":"plan или fact",'
        '"unit":"подразделение-заказчик или пустая строка",'
        '"owner":"владелец/команда если назван, иначе пустая строка",'
        '"tags":["до 5 коротких тегов на русском"],'
        '"missing":["из списка problem, stage, effect — о чём в тексте нет сведений"],'
        f'"followup":{followup}}}\n'
        "Не выдумывай факты. Если сведений нет — пустая строка. Стадия research означает проработку."
    )


def extract_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def call_llm(prompt):
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
    }).encode()
    req = urllib.request.Request(LLM_URL + "/chat/completions", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    if LLM_API_KEY:
        req.add_header("Authorization", "Bearer " + LLM_API_KEY)
    ctx = None
    if LLM_URL.startswith("https"):
        ctx = ssl.create_default_context(cafile=LLM_CA_BUNDLE or None)
        if not LLM_VERIFY_TLS:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    # прокси из окружения не используем: LLM находится внутри контура
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=ctx))
    with opener.open(req, timeout=LLM_TIMEOUT) as r:
        data = json.loads(r.read().decode("utf-8"))
    return extract_json(data["choices"][0]["message"]["content"])


# ---------------------------------------------------------------- export / import
CSV_COLS = ["code", "title", "type", "stage", "direction", "owner", "unit", "problem",
            "solution", "effect", "effectKind", "tags", "wiki", "createdAt", "updatedAt"]


def to_csv(rows):
    buf = io.StringIO()
    buf.write("﻿")  # BOM — чтобы Excel открыл кириллицу
    w = csv.DictWriter(buf, fieldnames=CSV_COLS, delimiter=";", extrasaction="ignore")
    w.writeheader()
    for r in rows:
        r = dict(r, tags="; ".join(r.get("tags") or []))
        w.writerow(r)
    return buf.getvalue()


def read_import(path):
    raw = Path(path).read_bytes().decode("utf-8-sig")
    if path.lower().endswith(".json"):
        data = json.loads(raw)
        return data["cases"] if isinstance(data, dict) else data
    dialect = csv.Sniffer().sniff(raw.splitlines()[0], delimiters=";,\t")
    return list(csv.DictReader(io.StringIO(raw), dialect=dialect))


def import_rows(store, rows):
    n = 0
    for r in rows:
        if not s(r.get("title")):
            continue
        store.create(r, created_at=s(r.get("createdAt")) or None, updated_at=s(r.get("updatedAt")) or None)
        n += 1
    return n


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "InnoLib/" + VERSION
    store: Store = None

    def log_message(self, fmt, *args):
        log("%s %s" % (self.address_string(), fmt % args))

    # helpers
    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_err(self, status, code, message):
        self.send_json({"error": code, "message": message}, status)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("too_large")
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}

    def route(self):
        path = self.path.split("?", 1)[0]
        # работаем и за прокси с префиксом (/innolib/api/...): смотрим хвост пути
        m = re.search(r"/(api/.*|healthz)$", path)
        return (m.group(1) if m else None), path

    # verbs
    def do_GET(self):
        api, path = self.route()
        if api == "healthz":
            return self.send_json({"ok": True, "version": VERSION, "cases": self.store.count()})
        if api == "api/config":
            return self.send_json({"version": VERSION, "llm": llm_enabled(), "stages": STAGES, "directions": DIRECTIONS,
                                   "canDelete": bool(ADMIN_TOKEN)})
        if api == "api/cases":
            return self.send_json({"cases": self.store.list()})
        if api == "api/export.json":
            body = json.dumps({"exportedAt": now_iso(), "cases": self.store.list()},
                              ensure_ascii=False, indent=2).encode()
            return self.send_file_bytes(body, "application/json", "innolib-export.json")
        if api == "api/export.csv":
            return self.send_file_bytes(to_csv(self.store.list()).encode("utf-8"),
                                        "text/csv; charset=utf-8", "innolib-export.csv")
        if api:
            return self.send_err(404, "not_found", "Нет такого адреса")
        return self.serve_static(path)

    def do_POST(self):
        api, _ = self.route()
        try:
            body = self.read_json()
        except ValueError:
            return self.send_err(400, "bad_request", "Некорректный JSON или слишком большой запрос")
        if api == "api/cases":
            return self.send_json(self.store.create(body), 201)
        if api == "api/parse":
            if not llm_enabled():
                return self.send_err(503, "llm_disabled", "LLM не настроен (LLM_URL, LLM_MODEL)")
            try:
                t = time.time()
                result = call_llm(build_prompt(body))
                log(f"LLM ok in {time.time() - t:.1f}s")
                return self.send_json(result if isinstance(result, dict) else {})
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                log(f"LLM network error: {e}")
                return self.send_err(502, "llm_unreachable", "LLM не отвечает")
            except (ValueError, KeyError, IndexError) as e:
                log(f"LLM bad response: {e}")
                return self.send_err(502, "llm_bad_response", "LLM вернул ответ не в формате JSON")
        return self.send_err(404, "not_found", "Нет такого адреса")

    def do_PATCH(self):
        api, _ = self.route()
        m = re.fullmatch(r"api/cases/([\w-]+)", api or "")
        if not m:
            return self.send_err(404, "not_found", "Нет такого адреса")
        try:
            body = self.read_json()
        except ValueError:
            return self.send_err(400, "bad_request", "Некорректный JSON")
        doc = self.store.update(m.group(1), body)
        return self.send_json(doc) if doc else self.send_err(404, "not_found", "Кейс не найден")

    def do_DELETE(self):
        api, _ = self.route()
        m = re.fullmatch(r"api/cases/([\w-]+)", api or "")
        if not m:
            return self.send_err(404, "not_found", "Нет такого адреса")
        if not ADMIN_TOKEN or self.headers.get("X-Admin-Token") != ADMIN_TOKEN:
            return self.send_err(403, "forbidden", "Удаление доступно только администратору")
        ok = self.store.delete(m.group(1))
        return self.send_json({"ok": ok}) if ok else self.send_err(404, "not_found", "Кейс не найден")

    def send_file_bytes(self, body, ctype, filename):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def serve_static(self, path):
        rel = path.rsplit("/static/", 1)[-1] if "/static/" in path else ""
        target = (STATIC_DIR / rel).resolve() if rel else STATIC_DIR / "index.html"
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR / "index.html":
            return self.send_err(404, "not_found", "Нет такого файла")
        if not target.is_file():
            target = STATIC_DIR / "index.html"  # SPA: любой неизвестный путь — главная
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".woff2":
            ctype = "font/woff2"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400" if rel.startswith("fonts/") else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


def main():
    store = Store(DB_PATH)
    args = sys.argv[1:]
    if args and args[0] == "import":
        if len(args) < 2:
            sys.exit("Использование: python3 app.py import файл.csv|файл.json")
        n = import_rows(store, read_import(args[1]))
        print(f"Загружено кейсов: {n}. Всего в реестре: {store.count()}")
        return
    if args and args[0] == "export":
        print(json.dumps({"exportedAt": now_iso(), "cases": store.list()}, ensure_ascii=False, indent=2))
        return
    if args and args[0] not in ("serve",):
        sys.exit("Команды: serve (по умолчанию), import <файл>, export")

    if SEED_DEMO and store.count() == 0:
        seed = BASE_DIR / "data" / "seed_demo.json"
        if seed.exists():
            log(f"Пустая база — загружаю демо-кейсы: {import_rows(store, read_import(str(seed)))}")
    Handler.store = store
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"Библиотека инноваций {VERSION}: http://{HOST}:{PORT}  БД: {DB_PATH}  "
        f"LLM: {'вкл (' + LLM_MODEL + ')' if llm_enabled() else 'выкл'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
