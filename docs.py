"""Извлечение текста из документов базы знаний: TXT, MD, CSV, HTML, DOCX.

Только стандартная библиотека. PDF не поддерживается — сохраните как DOCX или TXT.
"""
import html
import io
import re
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree

SUPPORTED = (".txt", ".md", ".markdown", ".csv", ".html", ".htm", ".docx")
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocError(Exception):
    pass


def _decode(raw):
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table"}

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n\n" if tag != "br" else "\n")
        if tag == "li":
            self.out.append("- ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def _html(raw):
    p = _HTMLText()
    p.feed(_decode(raw))
    return html.unescape("".join(p.out))


def _docx(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            root = ElementTree.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        raise DocError("Файл DOCX повреждён или это не DOCX") from None
    body = root.find(_W + "body")
    lines = []

    def para_text(p):
        parts = []
        for el in p.iter():
            if el.tag == _W + "t" and el.text:
                parts.append(el.text)
            elif el.tag == _W + "tab":
                parts.append("\t")
            elif el.tag in (_W + "br", _W + "cr"):
                parts.append("\n")
        return "".join(parts)

    for block in (body if body is not None else []):
        if block.tag == _W + "p":
            t = para_text(block)
            style = block.find(f"{_W}pPr/{_W}pStyle")
            if style is not None and re.match(r"(?i)heading|заголовок", style.get(_W + "val", "")):
                t = "## " + t
            lines.append(t)
        elif block.tag == _W + "tbl":
            for row in block.iter(_W + "tr"):
                cells = [" ".join(para_text(p) for p in tc.iter(_W + "p")).strip() for tc in row.iter(_W + "tc")]
                lines.append(" | ".join(cells))
            lines.append("")
    return "\n\n".join(lines)


def extract_text(filename, raw):
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        raise DocError("PDF пока не поддерживается: сохраните документ как DOCX или TXT")
    if name.endswith(".docx"):
        text = _docx(raw)
    elif name.endswith((".html", ".htm")):
        text = _html(raw)
    elif name.endswith(SUPPORTED) or "." not in name:
        text = _decode(raw)
    else:
        raise DocError("Поддерживаются файлы: " + ", ".join(SUPPORTED))
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 20:
        raise DocError("В документе почти нет текста")
    return text
