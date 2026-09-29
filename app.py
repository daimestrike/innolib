#!/usr/bin/env python3
"""Библиотека инноваций — реестр кейсов и консультант по идеям.

Только стандартная библиотека Python 3.9+: никаких pip install,
работает в закрытом контуре без доступа в интернет.

Запуск:
    python3 app.py                       # веб-сервер
    python3 app.py import cases.csv      # загрузить кейсы из CSV/JSON
    python3 app.py export > backup.json  # выгрузить реестр
    python3 app.py add-doc файл.docx ... # добавить документы в базу знаний
    python3 app.py check-llm             # проверить подключение к LLM
    python3 app.py reindex               # пересчитать эмбеддинги (если EMBED_MODEL задан)

Настройки — через переменные окружения или файл .env рядом с app.py (см. .env.example).
"""
import base64
import csv
import io
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))


def load_dotenv(path):
    """Мини-загрузчик .env: переменные окружения, заданные явно, важнее файла."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        os.environ.setdefault(k, v)


load_dotenv(BASE_DIR / ".env")

from docs import DocError, extract_text  # noqa: E402
from llm import LLM, LLMError, extract_json  # noqa: E402
from rag import Rag, build_messages, retrieval_query  # noqa: E402

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
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "6"))

MAX_BODY = 1_000_000
MAX_DOC_BODY = 25_000_000

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
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS cases(
                    id TEXT PRIMARY KEY, code TEXT UNIQUE, data TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS docs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, filename TEXT,
                    text TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS embeddings(
                    hash TEXT PRIMARY KEY, model TEXT NOT NULL, vec TEXT NOT NULL);
            """)
            self.conn.commit()

    # -------- cases
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

    # -------- knowledge base docs
    @staticmethod
    def _doc(r, with_text):
        d = {"id": r["id"], "code": f"DOC-{r['id']}", "title": r["title"], "filename": r["filename"],
             "chars": len(r["text"]), "createdAt": r["created_at"]}
        if with_text:
            d["text"] = r["text"]
        return d

    def list_docs(self, with_text=False):
        with self.lock:
            rows = self.conn.execute("SELECT * FROM docs ORDER BY id").fetchall()
        return [self._doc(r, with_text) for r in rows]

    def count_docs(self):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]

    def add_doc(self, title, filename, text):
        with self.lock:
            cur = self.conn.execute("INSERT INTO docs(title, filename, text, created_at) VALUES(?,?,?,?)",
                                    (title[:200], filename[:200], text, now_iso()))
            self.conn.commit()
            r = self.conn.execute("SELECT * FROM docs WHERE id=?", (cur.lastrowid,)).fetchone()
        return self._doc(r, False)

    def delete_doc(self, did):
        with self.lock:
            n = self.conn.execute("DELETE FROM docs WHERE id=?", (did,)).rowcount
            self.conn.commit()
        return n > 0

    # -------- embeddings cache
    def load_embeddings(self, model):
        with self.lock:
            rows = self.conn.execute("SELECT hash, vec FROM embeddings WHERE model=?", (model,)).fetchall()
        return {r["hash"]: json.loads(r["vec"]) for r in rows}

    def save_embeddings(self, model, vecs):
        with self.lock:
            self.conn.executemany("INSERT OR REPLACE INTO embeddings VALUES(?,?,?)",
                                  [(h, model, json.dumps(v)) for h, v in vecs.items()])
            self.conn.commit()


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


# ---------------------------------------------------------------- card parsing prompt
def build_parse_prompt(p):
    parts = []
    if p.get("wikiText"):
        parts.append("ТЕКСТ СТРАНИЦЫ ВИКИ:\n" + s(p["wikiText"], 12000) + "\n")
    if p.get("chat"):
        parts.append("ДИАЛОГ С КОНСУЛЬТАНТОМ (извлеки из него суть идеи сотрудника):\n" + s(p["chat"], 12000) + "\n")
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


# ---------------------------------------------------------------- export / import
CSV_COLS = ["code", "title", "type", "stage", "direction", "owner", "unit", "problem",
            "solution", "effect", "effectKind", "tags", "wiki", "createdAt", "updatedAt"]


def to_csv(rows):
    buf = io.StringIO()
    buf.write("﻿")  # BOM — чтобы Excel открыл кириллицу
    w = csv.DictWriter(buf, fieldnames=CSV_COLS, delimiter=";", extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(dict(r, tags="; ".join(r.get("tags") or [])))
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
    llm: LLM = None
    rag: Rag = None

    def log_message(self, fmt, *args):
        log("%s %s" % (self.address_string(), fmt % args))

    # -------- helpers
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

    def read_json(self, limit=MAX_BODY):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("too_large")
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}

    def route(self):
        path = self.path.split("?", 1)[0]
        # работаем и за прокси с префиксом (/innolib/api/...): смотрим хвост пути
        m = re.search(r"/(api/.*|healthz)$", path)
        return (m.group(1) if m else None), path

    def query_param(self, name):
        from urllib.parse import parse_qs, urlsplit
        return (parse_qs(urlsplit(self.path).query).get(name) or [""])[0]

    def is_admin(self):
        return bool(ADMIN_TOKEN) and self.headers.get("X-Admin-Token") == ADMIN_TOKEN

    def changed(self):
        self.rag.invalidate()

    # -------- verbs
    def do_GET(self):
        api, path = self.route()
        if api == "healthz":
            return self.send_json({"ok": True, "version": VERSION, "cases": self.store.count()})
        if api == "api/config":
            return self.send_json({
                "version": VERSION, "llm": self.llm.enabled, "chatModel": self.llm.chat_model if self.llm.enabled else "",
                "embeddings": self.llm.embeddings_enabled, "stages": STAGES, "directions": DIRECTIONS,
                "canDelete": bool(ADMIN_TOKEN)})
        if api == "api/cases":
            return self.send_json({"cases": self.store.list()})
        if api == "api/docs":
            return self.send_json({"docs": self.store.list_docs()})
        if api == "api/search":
            q = self.query_param("q")
            return self.send_json({"results": [
                {k: v for k, v in r.items() if k != "boost"} for r in self.rag.search(q, RAG_TOP_K)]})
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
            body = self.read_json(MAX_DOC_BODY if api == "api/docs" else MAX_BODY)
        except ValueError:
            return self.send_err(400, "bad_request", "Некорректный JSON или слишком большой запрос")
        if api == "api/cases":
            doc = self.store.create(body)
            self.changed()
            return self.send_json(doc, 201)
        if api == "api/parse":
            return self.handle_parse(body)
        if api == "api/chat":
            return self.handle_chat(body)
        if api == "api/docs":
            return self.handle_add_doc(body)
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
        if doc:
            self.changed()
        return self.send_json(doc) if doc else self.send_err(404, "not_found", "Кейс не найден")

    def do_DELETE(self):
        api, _ = self.route()
        m = re.fullmatch(r"api/(cases|docs)/([\w-]+)", api or "")
        if not m:
            return self.send_err(404, "not_found", "Нет такого адреса")
        if not self.is_admin():
            return self.send_err(403, "forbidden", "Удаление доступно только администратору")
        if m.group(1) == "cases":
            ok = self.store.delete(m.group(2))
        else:
            ok = m.group(2).isdigit() and self.store.delete_doc(int(m.group(2)))
        if ok:
            self.changed()
        return self.send_json({"ok": True}) if ok else self.send_err(404, "not_found", "Не найдено")

    # -------- handlers
    def handle_parse(self, body):
        if not self.llm.enabled:
            return self.send_err(503, "llm_disabled", "LLM не настроен (LLM_URL, LLM_MODEL)")
        try:
            t = time.time()
            text = self.llm.complete([{"role": "user", "content": build_parse_prompt(body)}], temperature=0.1)
            result = extract_json(text)
            log(f"parse ok in {time.time() - t:.1f}s")
            return self.send_json(result if isinstance(result, dict) else {})
        except LLMError as e:
            log(f"parse: {e}")
            return self.send_err(502, e.code, str(e))
        except ValueError:
            return self.send_err(502, "llm_bad_response", "LLM вернул ответ не в формате JSON")

    def handle_add_doc(self, body):
        filename = s(body.get("filename"), 200) or "document.txt"
        title = s(body.get("title"), 200) or re.sub(r"\.[^.]+$", "", filename)
        try:
            if body.get("text"):
                text = extract_text("x.txt", str(body["text"]).encode("utf-8"))
            else:
                raw = base64.b64decode(body.get("contentBase64") or "", validate=False)
                text = extract_text(filename, raw)
        except DocError as e:
            return self.send_err(400, "bad_document", str(e))
        doc = self.store.add_doc(title, filename, text)
        self.changed()
        log(f"doc added {doc['code']} «{title}» ({len(text)} симв.)")
        return self.send_json(doc, 201)

    def sse(self, obj):
        self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def handle_chat(self, body):
        history = [m for m in (body.get("messages") or []) if isinstance(m, dict)][-30:]
        if not history or history[-1].get("role") != "user":
            return self.send_err(400, "bad_request", "Последнее сообщение должно быть от пользователя")
        found = self.rag.search(retrieval_query(history), RAG_TOP_K)
        sources = [{"ref": f["ref"], "kind": f["kind"], "title": f["title"], "stage": f.get("stage"),
                    "caseId": f.get("caseId"), "snippet": f["text"][:600]} for f in found]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")  # nginx: не буферизовать поток
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        gen = None
        try:
            self.sse({"type": "sources", "sources": sources})
            if not self.llm.enabled:
                text = ("Консультант не подключён: на сервере не задан LLM (LLM_URL, LLM_MODEL). "
                        "Ниже — что нашлось в реестре по вашему вопросу.")
                if not found:
                    text = "Консультант не подключён, и по вашему вопросу в реестре ничего не нашлось."
                self.sse({"type": "delta", "text": text})
                self.sse({"type": "done"})
                return
            cases = self.store.list()
            msgs = build_messages(history, cases, found, self.store.count_docs())
            t = time.time()
            gen = self.llm.stream(msgs, temperature=0.3)
            n = 0
            for piece in gen:
                n += len(piece)
                self.sse({"type": "delta", "text": piece})
            log(f"chat ok: {n} симв. за {time.time() - t:.1f}s, источников {len(found)}")
            self.sse({"type": "done"})
        except LLMError as e:
            log(f"chat: {e}")
            try:
                self.sse({"type": "error", "code": e.code, "message": str(e)})
            except OSError:
                pass
        except (BrokenPipeError, ConnectionResetError):
            log("chat: клиент прервал ответ")
        finally:
            if gen is not None:
                gen.close()

    # -------- files
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
            target = STATIC_DIR / "index.html"  # любой неизвестный путь — главная
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


# ---------------------------------------------------------------- CLI
def check_llm(llm):
    if not llm.enabled:
        print("LLM не настроен: задайте LLM_URL и LLM_MODEL (в .env или окружении).")
        return 1
    print(f"Адрес: {llm.url}\nМодель: {llm.chat_model}\nКлюч: {'задан' if llm.key else 'не задан'}")
    ok = True
    try:
        ids = llm.models()
        mark = "есть" if llm.chat_model in ids else "НЕ НАЙДЕНА в списке"
        print(f"[ok] /models: {len(ids)} моделей, {llm.chat_model} — {mark}")
    except LLMError as e:
        print(f"[!] /models: {e}")
    try:
        t = time.time()
        ans = llm.complete([{"role": "user", "content": "Ответь одним словом: работает?"}])
        print(f"[ok] ответ за {time.time() - t:.1f} c: {ans[:80]!r}")
    except LLMError as e:
        ok = False
        print(f"[x] chat/completions: {e}")
    try:
        t = time.time()
        text = "".join(llm.stream([{"role": "user", "content": "Назови три цвета через запятую."}]))
        print(f"[ok] потоковый ответ за {time.time() - t:.1f} c: {text[:80]!r}")
    except LLMError as e:
        ok = False
        print(f"[x] stream: {e}")
    if llm.embeddings_enabled:
        try:
            v = llm.embed(["проверка"])[0]
            print(f"[ok] эмбеддинги {llm.embed_model}: размерность {len(v)}")
        except LLMError as e:
            ok = False
            print(f"[x] эмбеддинги: {e}")
    else:
        print("[i] EMBED_MODEL не задан — поиск работает на BM25 (этого достаточно для сотен кейсов)")
    return 0 if ok else 1


def main():
    store = Store(DB_PATH)
    llm = LLM(os.environ)
    rag = Rag(store, llm, log)
    args = sys.argv[1:]
    cmd = args[0] if args else "serve"

    if cmd == "import":
        if len(args) < 2:
            sys.exit("Использование: python3 app.py import файл.csv|файл.json")
        n = import_rows(store, read_import(args[1]))
        print(f"Загружено кейсов: {n}. Всего в реестре: {store.count()}")
        return
    if cmd == "export":
        print(json.dumps({"exportedAt": now_iso(), "cases": store.list()}, ensure_ascii=False, indent=2))
        return
    if cmd == "add-doc":
        if len(args) < 2:
            sys.exit("Использование: python3 app.py add-doc файл.docx [файл2.md ...]")
        for p in args[1:]:
            try:
                d = store.add_doc(Path(p).stem, Path(p).name, extract_text(p, Path(p).read_bytes()))
                print(f"{d['code']}: {d['title']} ({d['chars']} симв.)")
            except (DocError, OSError) as e:
                print(f"{p}: {e}")
        return
    if cmd == "check-llm":
        sys.exit(check_llm(llm))
    if cmd == "reindex":
        n = rag.warm() if llm.embeddings_enabled else 0
        print(f"Проиндексировано фрагментов: {len(rag.get_index().chunks)}, новых эмбеддингов: {n}")
        return
    if cmd != "serve":
        sys.exit("Команды: serve (по умолчанию), import, export, add-doc, check-llm, reindex")

    if SEED_DEMO and store.count() == 0:
        seed = BASE_DIR / "data" / "seed_demo.json"
        if seed.exists():
            log(f"Пустая база — загружаю демо-кейсы: {import_rows(store, read_import(str(seed)))}")
    Handler.store, Handler.llm, Handler.rag = store, llm, rag
    rag.warm_async()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    log(f"Библиотека инноваций {VERSION}: http://{HOST}:{PORT}  БД: {DB_PATH}  "
        f"LLM: {'вкл (' + llm.chat_model + ')' if llm.enabled else 'выкл'}  "
        f"поиск: {'BM25 + эмбеддинги ' + llm.embed_model if llm.embeddings_enabled else 'BM25'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
