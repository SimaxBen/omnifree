"""Ask my Drive: text extraction (PDF, text, HTML, .docx) and page-aware chunking.

Pure functions, no I/O: the router downloads the bytes and hands them over. Only ``pypdf``
(pure Python) is needed; HTML and .docx use the standard library.
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from xml.etree import ElementTree

__all__ = [
    "Chunk",
    "Extracted",
    "ExtractionError",
    "SUPPORTED_EXTENSIONS",
    "MIME_BY_EXTENSION",
    "extension_of",
    "extract",
    "chunk_pages",
    "html_to_text",
    "docx_to_text",
]

CHUNK_CHARS = 1000
CHUNK_OVERLAP = 150
MAX_DOCX_XML_BYTES = 30 * 1024 * 1024  # uncompressed word/document.xml (zip-bomb guard)

MIME_BY_EXTENSION: dict[str, str] = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
SUPPORTED_EXTENSIONS = frozenset(MIME_BY_EXTENSION)


class ExtractionError(Exception):
    """The file cannot be turned into text (unsupported, encrypted, corrupt, empty)."""


@dataclass(frozen=True)
class Extracted:
    title: str | None
    pages: list[tuple[int | None, str]]  # (1-based page number for PDFs else None, text)


@dataclass(frozen=True)
class Chunk:
    ord: int
    page: int | None
    text: str


def extension_of(key: str) -> str:
    return posixpath.splitext(key.lower())[1]


# --------------------------------------------------------------------------- decoders


def _decode_text(data: bytes) -> str:
    encodings = ("utf-16", "utf-8-sig") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",)
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("cp1252", errors="replace")


class _TextHTMLParser(HTMLParser):
    _SKIP = frozenset({"script", "style", "noscript", "template", "svg", "head"})
    _BLOCK = frozenset({
        "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "hr", "dd", "dt", "main", "nav",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append("\t")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in self._SKIP:
            self._skip = max(self._skip - 1, 0)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str | None, str]:
    """(title, visible text) with scripts/styles dropped and block tags turned into newlines."""
    parser = _TextHTMLParser()
    parser.feed(html)
    parser.close()
    title = " ".join("".join(parser.title).split()) or None
    return title, "".join(parser.parts)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DC_TITLE = "{http://purl.org/dc/elements/1.1/}title"


def docx_to_text(data: bytes) -> tuple[str | None, str]:
    """(title, text) of a .docx: paragraphs of word/document.xml, tabs and line breaks kept."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ExtractionError("not a valid .docx (zip) file") from exc
    with archive:
        try:
            info = archive.getinfo("word/document.xml")
        except KeyError as exc:
            raise ExtractionError("not a .docx: word/document.xml missing") from exc
        if info.file_size > MAX_DOCX_XML_BYTES:
            raise ExtractionError("document.xml too large")
        try:
            root = ElementTree.fromstring(archive.read(info))
        except ElementTree.ParseError as exc:
            raise ExtractionError("corrupt document.xml") from exc
        title = None
        if "docProps/core.xml" in archive.namelist():
            try:
                core = ElementTree.fromstring(archive.read("docProps/core.xml"))
                node = core.find(_DC_TITLE)
                title = (node.text or "").strip() or None if node is not None else None
            except ElementTree.ParseError:
                title = None
    paragraphs: list[str] = []
    for para in root.iter(f"{_W}p"):
        parts: list[str] = []
        for node in para.iter():
            if node.tag == f"{_W}t":
                parts.append(node.text or "")
            elif node.tag == f"{_W}tab":
                parts.append("\t")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return title, "\n".join(paragraphs)


def _pdf_pages(data: bytes, max_pages: int | None = None) -> tuple[str | None, list[tuple[int | None, str]]]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    raise ExtractionError("PDF is password-protected")
            except ExtractionError:
                raise
            except Exception as exc:
                raise ExtractionError("PDF is encrypted") from exc
        title = None
        try:
            meta = reader.metadata
            title = (str(meta.title).strip() or None) if meta and meta.title else None
        except Exception:
            title = None
        pages: list[tuple[int | None, str]] = []
        for number, page in enumerate(reader.pages, start=1):
            if max_pages is not None and number > max_pages:
                break
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            pages.append((number, text))
    except ExtractionError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise ExtractionError(f"unreadable PDF: {exc}") from exc
    return title, pages


def extract(key: str, data: bytes) -> Extracted:
    """Text of ``data`` by the extension of ``key``. Raises ExtractionError."""
    ext = extension_of(key)
    if ext not in SUPPORTED_EXTENSIONS:
        raise ExtractionError(f"unsupported file type '{ext or '(none)'}'")
    title: str | None = None
    if ext == ".pdf":
        title, pages = _pdf_pages(data)
    elif ext == ".docx":
        title, text = docx_to_text(data)
        pages = [(None, text)]
    elif ext in (".html", ".htm"):
        title, text = html_to_text(_decode_text(data))
        pages = [(None, text)]
    elif ext == ".json":
        text = _decode_text(data)
        try:
            text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
        except ValueError:
            pass
        pages = [(None, text)]
    else:
        pages = [(None, _decode_text(data))]
    pages = [(page, _normalize(text)) for page, text in pages]
    if not any(text for _page, text in pages):
        raise ExtractionError("no extractable text (scanned PDF or empty file?)")
    return Extracted(title=title, pages=pages)


# --------------------------------------------------------------------------- chunking

_SPACES_RE = re.compile(r"[ \t\f\v ]+")
_BLANKS_RE = re.compile(r"\n\s*\n\s*\n+")


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "\n".join(_SPACES_RE.sub(" ", line).strip() for line in text.split("\n"))
    return _BLANKS_RE.sub("\n\n", text).strip()


def _split(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text] if text.strip() else []
    out: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            window = text[start + size // 2 : end]
            for sep in ("\n\n", "\n", ". ", " "):
                cut = window.rfind(sep)
                if cut != -1:
                    end = start + size // 2 + cut + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        nxt = max(end - overlap, start + 1)
        space = text.find(" ", nxt, end)  # do not start the overlap mid-word
        start = space + 1 if space != -1 else nxt
    return out


def chunk_pages(
    pages: list[tuple[int | None, str]],
    *,
    size: int = CHUNK_CHARS,
    overlap: int = CHUNK_OVERLAP,
    max_chunks: int | None = None,
) -> list[Chunk]:
    """~``size``-char chunks with ``overlap``; chunks never span PDF pages so page numbers stay exact."""
    chunks: list[Chunk] = []
    for page, text in pages:
        for piece in _split(text, size, overlap):
            chunks.append(Chunk(ord=len(chunks), page=page, text=piece))
            if max_chunks is not None and len(chunks) >= max_chunks:
                return chunks
    return chunks
