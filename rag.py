"""Поиск по реестру и базе знаний для консультанта (RAG).

Основа — BM25 с лёгким стеммингом для русского: работает без моделей и сети.
Если задан EMBED_MODEL, результаты BM25 объединяются с векторным поиском
(reciprocal rank fusion). Векторы считаются в фоне и кэшируются в SQLite.
"""
import hashlib
import math
import re
import threading
from collections import Counter

STAGE_NAMES = {"idea": "Идея", "research": "Проработка", "pilot": "Пилот",
               "launched": "Внедрено", "stopped": "Остановлено"}

_STOP = set("""а без более бы был была были было быть в вам вас весь во вот все всего всех вы где да даже для до
его ее ей ему если есть еще же за здесь и из или им их к как ко когда кто ли либо меня мне может мы на надо наш
не него нее нет ни них но ну о об однако он она они оно от очень по под при про с со так также такой там те тем
то того тоже той только том ты у уже хотя чего чей чем что чтобы чье чья эта эти это этот я можно нужно какие
какой каких какая есть было будет the and for with of to in on is are""".split())

_SUFFIXES = sorted(set("""иями ями ами ого его ому ему ыми ими ая яя ое ее ые ие ый ий ой ую юю ов ев ей ам ям
ах ях ом ем ию ия ья ье ью ьи ться тся ть ти ешь ет ют ут им ит ат ят ал ил ла ли ло ость ости остью ение
ения ений ению ением ениями ание ания аний анию анием аниями ация ации ацию ацией аций ировать ирование
ирования ированный ированных а я о е ы и у ю ь й""".split()), key=len, reverse=True)

# короткие синонимы и сокращения, которые часто пишут в вопросах
_EXPAND = {
    "cv": "компьютерное зрение видео камеры",
    "кз": "компьютерное зрение",
    "ии": "genai нейросеть ассистент",
    "ai": "genai нейросеть",
    "llm": "genai ассистент",
    "gpt": "genai ассистент",
    "ocr": "распознавание документы",
    "vlm": "распознавание документы",
    "фрод": "антифрод мошенничество потери",
    "ксо": "касса самообслуживания",
    "бот": "ассистент чат-бот",
}


def stem(word):
    if not re.search("[а-я]", word):
        return word[:-1] if len(word) > 4 and word.endswith("s") else word
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[:-len(suf)]
    return word


def tokenize(text, expand=False):
    words = re.findall(r"[a-zа-я0-9]+", (text or "").lower().replace("ё", "е"))
    out = []
    for w in words:
        if expand and w in _EXPAND:
            out.extend(tokenize(_EXPAND[w]))
        if len(w) < 2 or w in _STOP:
            continue
        out.append(stem(w))
    return out


# ---------------------------------------------------------------- chunks
def case_chunk(c):
    lines = [f"[{c['code']}] {c['title']}",
             f"Тип: {'идея' if c.get('type') == 'idea' else 'проект'} · Стадия: {STAGE_NAMES.get(c.get('stage'), c.get('stage'))}"
             f" · Направление: {c.get('direction') or '—'}"]
    if c.get("unit") or c.get("owner"):
        lines.append(f"Подразделение: {c.get('unit') or '—'} · Владелец: {c.get('owner') or '—'}")
    for label, key in (("Проблема", "problem"), ("Решение", "solution")):
        if c.get(key):
            lines.append(f"{label}: {c[key]}")
    if c.get("effect"):
        lines.append(f"Эффект ({'факт' if c.get('effectKind') == 'fact' else 'план'}): {c['effect']}")
    if c.get("tags"):
        lines.append("Теги: " + ", ".join(c["tags"]))
    if c.get("wiki"):
        lines.append("Вики: " + c["wiki"])
    return {"ref": c["code"], "kind": "case", "caseId": c["id"], "title": c["title"],
            "stage": c.get("stage"), "text": "\n".join(lines),
            "boost": f"{c['title']} {c['title']} {' '.join(c.get('tags') or [])} {c.get('direction', '')}"}


def split_text(text, size=1200, overlap=200):
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        while len(p) > size:  # очень длинный абзац режем по предложениям
            cut = p.rfind(". ", 0, size)
            cut = cut + 1 if cut > size // 2 else size
            paras_piece, p = p[:cut].strip(), p[cut:].strip()
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(paras_piece)
        if cur and len(cur) + len(p) + 2 > size:
            chunks.append(cur)
            cur = cur[-overlap:].split(" ", 1)[-1] + "\n\n" + p if overlap else p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def doc_chunks(d):
    parts = split_text(d["text"])
    return [{"ref": d["code"], "kind": "doc", "docId": d["id"], "title": d["title"],
             "text": f"[{d['code']}] {d['title']} (фрагмент {i + 1}/{len(parts)})\n{t}",
             "boost": d["title"]} for i, t in enumerate(parts)]


# ---------------------------------------------------------------- index
class BM25:
    def __init__(self, chunks, k1=1.4, b=0.75):
        self.chunks = chunks
        self.k1, self.b = k1, b
        self.tf = [Counter(tokenize(c["text"] + " " + c.get("boost", ""))) for c in chunks]
        self.len = [sum(t.values()) for t in self.tf]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 1
        df = Counter()
        for t in self.tf:
            df.update(t.keys())
        n = len(chunks)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}

    def search(self, query, k):
        q = Counter(tokenize(query, expand=True))
        scores = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for w, qn in q.items():
                f = tf.get(w)
                if f:
                    s += self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
            if s > 0:
                scores.append((s, i))
        scores.sort(reverse=True)
        return scores[:k]


def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _hash(model, text):
    return hashlib.sha1((model + "\n" + text).encode("utf-8")).hexdigest()


class Rag:
    def __init__(self, store, llm, log):
        self.store, self.llm, self.log = store, llm, log
        self.lock = threading.Lock()
        self.index = None
        self.dirty = True
        self.vec_cache = {}
        self.warming = False
        if llm.embeddings_enabled:
            self.vec_cache = store.load_embeddings(llm.embed_model)

    def invalidate(self):
        with self.lock:
            self.dirty = True
        self.warm_async()

    def _build(self):
        chunks = [case_chunk(c) for c in self.store.list()]
        for d in self.store.list_docs(with_text=True):
            chunks.extend(doc_chunks(d))
        return BM25(chunks)

    def get_index(self):
        with self.lock:
            if self.dirty or self.index is None:
                self.index = self._build()
                self.dirty = False
            return self.index

    # -------- embeddings (необязательно)
    def warm(self):
        if not self.llm.embeddings_enabled:
            return 0
        idx = self.get_index()
        model = self.llm.embed_model
        todo = [(h, c["text"]) for c in idx.chunks
                for h in [_hash(model, c["text"])] if h not in self.vec_cache]
        if not todo:
            return 0
        vecs = self.llm.embed([t for _, t in todo])
        new = {h: v for (h, _), v in zip(todo, vecs)}
        self.vec_cache.update(new)
        self.store.save_embeddings(model, new)
        return len(new)

    def warm_async(self):
        if not self.llm.embeddings_enabled or self.warming:
            return

        def run():
            self.warming = True
            try:
                n = self.warm()
                if n:
                    self.log(f"RAG: посчитаны эмбеддинги для {n} фрагментов")
            except Exception as e:  # сеть/модель — не критично, остаётся BM25
                self.log(f"RAG: эмбеддинги недоступны ({e}), работаю на BM25")
            finally:
                self.warming = False
        threading.Thread(target=run, daemon=True).start()

    # -------- поиск
    def search(self, query, k=6):
        idx = self.get_index()
        if not idx.chunks or not query.strip():
            return []
        bm = idx.search(query, k * 3)
        ranked = {i: 1 / (60 + r) for r, (_, i) in enumerate(bm)}
        if self.llm.embeddings_enabled and self.vec_cache:
            try:
                qv = self.llm.embed([query])[0]
                model = self.llm.embed_model
                sims = []
                for i, c in enumerate(idx.chunks):
                    v = self.vec_cache.get(_hash(model, c["text"]))
                    if v:
                        sims.append((_cos(qv, v), i))
                sims.sort(reverse=True)
                for r, (s, i) in enumerate(sims[:k * 3]):
                    if s > 0.2:
                        ranked[i] = ranked.get(i, 0) + 1 / (60 + r)
            except Exception as e:
                self.log(f"RAG: векторный поиск пропущен ({e})")
        top = sorted(ranked.items(), key=lambda x: -x[1])
        out, seen_case = [], set()
        for i, score in top:
            c = idx.chunks[i]
            key = c["ref"] if c["kind"] == "case" else c["text"][:80]
            if key in seen_case:
                continue
            seen_case.add(key)
            out.append(dict(c, score=round(score, 5)))
            if len(out) >= k:
                break
        return out


# ---------------------------------------------------------------- prompt
SYSTEM_PROMPT = """Ты — консультант по инновациям в розничной сети X5. Помогаешь сотрудникам искать, развивать и проверять идеи.

Как работать:
- Опирайся на реестр проектов и базу знаний ниже. Ссылайся на кейсы их кодом в квадратных скобках, например [INN-012], на документы — [DOC-3]. Коды бери только из контекста.
- Если в контексте нет ответа, прямо скажи об этом и отвечай из общих знаний, явно пометив: «вне реестра».
- Когда человек приносит идею: найди похожие проекты и скажи, что уже сделано; чем идея отличается; какие риски; как быстро проверить гипотезу (пилот, метрика успеха, масштаб); кого привлечь — владельцев похожих проектов из реестра.
- Предлагая новые идеи, отталкивайся от существующих решений: что можно переиспользовать, масштабировать или перенести в другое подразделение.
- Если вопрос размытый, задай один уточняющий вопрос.
- Не выдумывай кейсы, коды, цифры эффекта и имена.
- Отвечай по-русски, по делу, коротко. Списки — когда перечисляешь варианты или шаги."""


def build_context(cases, found, docs_count):
    by_stage = Counter(c.get("stage") for c in cases)
    by_dir = Counter(c.get("direction") for c in cases)
    parts = [
        f"СВОДКА РЕЕСТРА: всего {len(cases)} кейсов, документов в базе знаний: {docs_count}.",
        "По стадиям: " + ", ".join(f"{STAGE_NAMES.get(s, s)} — {n}" for s, n in by_stage.most_common()),
        "По направлениям: " + ", ".join(f"{d} — {n}" for d, n in by_dir.most_common()),
        "",
        "КАТАЛОГ (код · название · стадия · направление):",
    ]
    for c in cases[:300]:
        parts.append(f"{c['code']} · {c['title']} · {STAGE_NAMES.get(c.get('stage'), '')} · {c.get('direction', '')}")
    if len(cases) > 300:
        parts.append(f"… и ещё {len(cases) - 300}")
    parts.append("")
    if found:
        parts.append("НАЙДЕНО ПО ЗАПРОСУ (подробно):")
        for f in found:
            parts.append(f["text"])
            parts.append("---")
    else:
        parts.append("По запросу подробных совпадений не нашлось — используй каталог.")
    return "\n".join(parts)


def build_messages(history, cases, found, docs_count, max_turns=12):
    msgs = [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": build_context(cases, found, docs_count)}]
    for m in history[-max_turns:]:
        role = m.get("role")
        if role in ("user", "assistant") and str(m.get("content", "")).strip():
            msgs.append({"role": role, "content": str(m["content"])[:6000]})
    return msgs


def retrieval_query(history):
    users = [str(m.get("content", "")) for m in history if m.get("role") == "user"]
    return " ".join(users[-2:])[-2000:]
