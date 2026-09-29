"""Клиент OpenAI-совместимого API на стандартной библиотеке Python.

Подходит для X5 CoPilot API, vLLM, Ollama, LiteLLM, TGI — всего, что отвечает
на /v1/chat/completions (и, опционально, /v1/embeddings).
"""
import json
import re
import ssl
import urllib.error
import urllib.request

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class LLMError(Exception):
    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code = code
        self.status = status


def strip_think(text):
    """Убирает блоки рассуждений <think>…</think> у reasoning-моделей."""
    text = _THINK_RE.sub("", text or "")
    if "</think>" in text:  # шаблон открыл <think> сам, в ответе только закрывающий тег
        text = text.split("</think>", 1)[1]
    return text.strip()


def extract_json(text):
    text = strip_think(text)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


class _ThinkFilter:
    """Вырезает <think>…</think> из потока по кусочкам."""
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf = ""
        self.inside = False

    @staticmethod
    def _partial(buf, tag):
        for k in range(len(tag) - 1, 0, -1):
            if buf.endswith(tag[:k]):
                return k
        return 0

    def feed(self, piece):
        self.buf += piece
        out = []
        while True:
            if self.inside:
                i = self.buf.find(self.CLOSE)
                if i < 0:
                    self.buf = self.buf[-len(self.CLOSE):]
                    break
                self.buf = self.buf[i + len(self.CLOSE):]
                self.inside = False
            else:
                i = self.buf.find(self.OPEN)
                if i < 0:
                    keep = self._partial(self.buf, self.OPEN)
                    out.append(self.buf[:len(self.buf) - keep])
                    self.buf = self.buf[len(self.buf) - keep:]
                    break
                out.append(self.buf[:i])
                self.buf = self.buf[i + len(self.OPEN):]
                self.inside = True
        return "".join(out)

    def flush(self):
        rest = "" if self.inside else self.buf
        self.buf = ""
        return rest


class LLM:
    def __init__(self, env):
        self.url = env.get("LLM_URL", "").strip().rstrip("/")
        self.model = env.get("LLM_MODEL", "").strip()
        self.chat_model = env.get("LLM_CHAT_MODEL", "").strip() or self.model
        self.key = env.get("LLM_API_KEY", "").strip()
        self.timeout = float(env.get("LLM_TIMEOUT", "90"))
        self.embed_model = env.get("EMBED_MODEL", "").strip()
        self.embed_url = (env.get("EMBED_URL", "").strip() or self.url).rstrip("/")
        extra = env.get("LLM_EXTRA_JSON", "").strip()
        try:
            self.extra = json.loads(extra) if extra else {}
        except ValueError:
            raise SystemExit("LLM_EXTRA_JSON: некорректный JSON")

        ctx = ssl.create_default_context(cafile=env.get("LLM_CA_BUNDLE", "").strip() or None)
        if env.get("LLM_VERIFY_TLS", "1") != "1":
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        handlers = [urllib.request.HTTPSHandler(context=ctx)]
        if env.get("LLM_USE_SYSTEM_PROXY", "0") != "1":
            handlers.append(urllib.request.ProxyHandler({}))  # API внутри контура — без прокси
        self.opener = urllib.request.build_opener(*handlers)

    @property
    def enabled(self):
        return bool(self.url and self.model)

    @property
    def embeddings_enabled(self):
        return bool(self.embed_url and self.embed_model)

    # ------------------------------------------------------------ transport
    def _open(self, base, path, body=None, stream=False, timeout=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(base + path, data=data, method="POST" if data else "GET")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "text/event-stream" if stream else "application/json")
        if self.key:
            req.add_header("Authorization", "Bearer " + self.key)
        try:
            return self.opener.open(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            code = {401: "llm_auth", 403: "llm_auth", 404: "llm_not_found",
                    429: "llm_rate_limited"}.get(e.code, "llm_http")
            raise LLMError(code, f"LLM вернул HTTP {e.code}: {detail}", e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMError("llm_unreachable", f"LLM недоступен: {e}") from None

    def _json(self, base, path, body=None, timeout=None):
        with self._open(base, path, body, timeout=timeout) as r:
            try:
                return json.loads(r.read().decode("utf-8"))
            except ValueError:
                raise LLMError("llm_bad_response", "LLM вернул не JSON") from None

    def _body(self, messages, temperature, stream):
        body = {"model": self.chat_model, "messages": messages, "temperature": temperature}
        if stream:
            body["stream"] = True
        body.update(self.extra)
        return body

    # ------------------------------------------------------------ API
    def models(self):
        data = self._json(self.url, "/models", timeout=15)
        return [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]

    def complete(self, messages, temperature=0.1, model=None):
        body = self._body(messages, temperature, False)
        if model:
            body["model"] = model
        data = self._json(self.url, "/chat/completions", body)
        try:
            return strip_think(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            raise LLMError("llm_bad_response", "В ответе LLM нет choices[0].message.content") from None

    def stream(self, messages, temperature=0.3):
        """Генератор кусочков ответа. Если сервер не умеет stream — отдаёт ответ целиком."""
        try:
            resp = self._open(self.url, "/chat/completions", self._body(messages, temperature, True), stream=True)
        except LLMError as e:
            if e.status in (400, 422):
                yield self.complete(messages, temperature)
                return
            raise
        filt = _ThinkFilter()
        with resp:
            if "text/event-stream" not in (resp.headers.get("Content-Type") or ""):
                try:
                    data = json.loads(resp.read().decode("utf-8"))
                    yield strip_think(data["choices"][0]["message"]["content"] or "")
                except (ValueError, KeyError, IndexError, TypeError):
                    raise LLMError("llm_bad_response", "LLM вернул неожиданный ответ") from None
                return
            try:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except ValueError:
                        continue
                    if obj.get("error"):
                        raise LLMError("llm_http", f"LLM: {obj['error']}")
                    choice = (obj.get("choices") or [{}])[0]
                    piece = (choice.get("delta") or {}).get("content") or ""
                    if piece:
                        out = filt.feed(piece)
                        if out:
                            yield out
            except (TimeoutError, OSError) as e:
                raise LLMError("llm_unreachable", f"Поток от LLM оборвался: {e}") from None
        tail = filt.flush()
        if tail:
            yield tail

    def embed(self, texts, batch=32):
        out = []
        for i in range(0, len(texts), batch):
            data = self._json(self.embed_url, "/embeddings",
                              {"model": self.embed_model, "input": texts[i:i + batch]})
            items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
            if len(items) != len(texts[i:i + batch]):
                raise LLMError("llm_bad_response", "Эмбеддинги: число векторов не совпадает с числом текстов")
            out.extend(d["embedding"] for d in items)
        return out
