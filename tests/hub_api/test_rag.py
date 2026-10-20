"""Ask my Drive: extraction, chunking, index / re-index / delete, ask with citations (no network)."""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

from hub_api.rag_extract import (
    ExtractionError,
    chunk_pages,
    docx_to_text,
    extract,
    html_to_text,
)
from hub_api.routers import rag as rag_router
from tests.hub_api import fakes, fakes_rag

R = "tests.hub_api.fakes_rag"
F = "tests.hub_api.fakes"


@pytest.fixture(autouse=True)
def _reset_rag_fakes() -> None:
    fakes_rag.reset()


def rag_plugins(**llm_settings: Any) -> dict[str, Any]:
    return {
        "ragllm": {"module": f"{R}:FakeRagLLM", "kind": "llm", "group": "llm_pool", "priority": 10,
                   "settings": {"default_model": "fake-chat", "embedding_models": ["fake-embed"], **llm_settings}},
        "drive": {"module": f"{F}:FakeStorage", "kind": "storage", "group": "storage", "priority": 10,
                  "settings": {"capacity_bytes": 10_000_000}},
        "turso": {"module": f"{R}:FakeTurso", "kind": "db", "group": "databases", "priority": 10},
    }


@pytest.fixture
def rag_hub(make_hub):
    return make_hub(rag_plugins())


def put(key: str, data: bytes, content_type: str | None = None) -> None:
    fakes.STORES["drive"][key] = (data, content_type)


# --------------------------------------------------------------------------- fixtures: tiny files


def make_pdf(pages: list[str], title: str | None = None) -> bytes:
    """A minimal valid PDF (Helvetica text, one line per page) built by hand."""
    objects: list[bytes] = []
    n_pages = len(pages)
    font_id = 3 + 2 * n_pages
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n_pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode()
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    info_id = None
    if title:
        objects.append(f"<< /Title ({title}) >>".encode())
        info_id = len(objects)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    info = f" /Info {info_id} 0 R" if info_id else ""
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R{info} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def make_docx(paragraphs: list[str], title: str | None = None) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    body += "<w:p><w:r><w:t>a</w:t><w:tab/><w:t>b</w:t><w:br/><w:t>c</w:t></w:r></w:p>"
    doc = f'<?xml version="1.0"?><w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
        if title:
            z.writestr(
                "docProps/core.xml",
                '<?xml version="1.0"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/'
                'package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                f"<dc:title>{title}</dc:title></cp:coreProperties>",
            )
    return buf.getvalue()


# --------------------------------------------------------------------------- extraction


def test_extract_pdf_keeps_page_numbers_and_title():
    out = extract("docs/fruit.pdf", make_pdf(["Apples are red.", "Bananas are yellow."], title="Fruit guide"))
    assert out.title == "Fruit guide"
    assert [p for p, _ in out.pages] == [1, 2]
    assert "Apples are red" in out.pages[0][1]
    assert "Bananas" in out.pages[1][1]


def test_extract_text_markdown_csv_json():
    assert extract("a.txt", "héllo   world\r\n".encode()).pages == [(None, "héllo world")]
    assert extract("a.md", b"# Title\n\nbody").pages[0][1] == "# Title\n\nbody"
    assert "x,y" in extract("a.csv", b"x,y\n1,2\n").pages[0][1]
    assert '"k": "v"' in extract("a.json", b'{"k":"v"}').pages[0][1]
    assert extract("bad.json", b"{not json").pages[0][1] == "{not json"
    assert extract("latin.txt", "caf\xe9".encode("cp1252")).pages[0][1] == "café"
    assert extract("bom.txt", "hi".encode("utf-16")).pages[0][1] == "hi"


def test_extract_html_strips_tags_scripts_and_styles():
    html = (
        "<html><head><title>My page</title><style>p{color:red}</style></head>"
        "<body><h1>Hello</h1><script>alert(1)</script><p>World &amp; more</p></body></html>"
    )
    title, text = html_to_text(html)
    assert title == "My page"
    assert "alert" not in text and "color" not in text
    out = extract("page.HTML", html.encode())
    assert out.title == "My page"
    assert out.pages[0][1] == "Hello\n\nWorld & more"


def test_extract_docx_with_zipfile():
    data = make_docx(["First paragraph.", "Second paragraph."], title="Report")
    title, text = docx_to_text(data)
    assert title == "Report"
    assert text.splitlines()[:2] == ["First paragraph.", "Second paragraph."]
    assert "a\tb\nc" in text
    assert extract("r.docx", data).title == "Report"


@pytest.mark.parametrize(
    ("key", "data", "message"),
    [
        ("x.exe", b"MZ", "unsupported"),
        ("x.pdf", b"not a pdf", "PDF"),
        ("x.docx", b"not a zip", "docx"),
        ("x.txt", b"   \n\n ", "no extractable text"),
    ],
)
def test_extract_errors(key, data, message):
    with pytest.raises(ExtractionError, match=message):
        extract(key, data)


def test_chunking_overlap_and_pages():
    words = " ".join(f"w{i}" for i in range(600))  # ~3,000 chars
    chunks = chunk_pages([(1, words), (2, "short page")], size=1000, overlap=150)
    assert [c.ord for c in chunks] == list(range(len(chunks)))
    page1 = [c for c in chunks if c.page == 1]
    assert len(page1) >= 3 and all(len(c.text) <= 1000 for c in page1)
    first, second = page1[0].text.split(), page1[1].text.split()
    assert first[-1] in second[:40]  # overlap carried into the next chunk
    assert all(not c.text.startswith(" ") for c in chunks)
    assert chunks[-1].page == 2 and chunks[-1].text == "short page"
    assert len(chunk_pages([(None, words)], size=100, overlap=10, max_chunks=5)) == 5
    assert chunk_pages([(None, "")]) == []


# --------------------------------------------------------------------------- index / docs / delete


def test_rag_requires_auth(rag_hub):
    assert rag_hub.client.get("/rag/docs").status_code == 401
    assert rag_hub.client.post("/rag/index", json={"provider": "drive", "key": "a.txt"}).status_code == 401
    assert rag_hub.client.post("/rag/ask", json={"question": "hi"}).status_code == 401
    assert rag_hub.client.delete("/rag/docs/drive/a.txt").status_code == 401


def test_index_pdf_then_list(rag_hub):
    put("docs/fruit.pdf", make_pdf(["Apples are red and crunchy.", "Bananas are yellow."]), "application/pdf")
    r = rag_hub.post("/rag/index", json={"provider": "drive", "key": "docs/fruit.pdf"})
    assert r.status_code == 200, r.text
    doc = r.json()["doc"]
    assert doc["chunks"] == 2 and doc["error"] is None
    assert doc["title"] == "fruit.pdf" and doc["mime"] == "application/pdf" and doc["size"] > 0
    assert doc["indexed_at"].endswith("Z")

    listing = rag_hub.get("/rag/docs").json()
    assert listing["embedding_model"] == "fake-embed"
    assert [d["key"] for d in listing["docs"]] == ["docs/fruit.pdf"]
    pages = fakes_rag.DBS["turso"].execute("select page from rag_chunks order by ord").fetchall()
    assert pages == [(1,), (2,)]
    assert any("F32_BLOB(64)" in s for s in fakes_rag.SQL_LOG)
    assert any("vector32(?)" in s for s in fakes_rag.SQL_LOG)


def test_index_batches_embeddings(rag_hub, monkeypatch):
    monkeypatch.setattr(rag_router, "EMBED_BATCH", 4)
    text = "\n\n".join(f"Paragraph {i} " + "lorem ipsum dolor " * 60 for i in range(10))
    put("long.md", text.encode())
    doc = rag_hub.post("/rag/index", json={"provider": "drive", "key": "long.md"}).json()["doc"]
    assert doc["chunks"] > 8
    assert fakes_rag.EMBED["inputs"] == doc["chunks"]
    assert fakes_rag.EMBED["calls"] == -(-doc["chunks"] // 4)
    count = fakes_rag.DBS["turso"].execute("select count(*) from rag_chunks").fetchone()[0]
    assert count == doc["chunks"]


def test_chunk_cap(rag_hub, monkeypatch):
    monkeypatch.setattr(rag_router, "MAX_CHUNKS", 3)
    put("big.txt", ("word " * 2000).encode())
    assert rag_hub.post("/rag/index", json={"provider": "drive", "key": "big.txt"}).json()["doc"]["chunks"] == 3


def test_reindex_replaces_chunks(rag_hub):
    put("notes.txt", b"old content about pears")
    rag_hub.post("/rag/index", json={"provider": "drive", "key": "notes.txt"})
    put("notes.txt", ("new content about plums. " * 80).encode())
    doc = rag_hub.post("/rag/index", json={"provider": "drive", "key": "notes.txt"}).json()["doc"]
    rows = fakes_rag.DBS["turso"].execute("select text from rag_chunks where key = 'notes.txt'").fetchall()
    assert len(rows) == doc["chunks"] > 1
    assert all("pears" not in r[0] for r in rows)
    assert len(rag_hub.get("/rag/docs").json()["docs"]) == 1


def test_index_errors_are_recorded(rag_hub, monkeypatch):
    put("tool.exe", b"MZ")
    doc = rag_hub.post("/rag/index", json={"provider": "drive", "key": "tool.exe"}).json()["doc"]
    assert doc["chunks"] == 0 and "unsupported" in doc["error"]

    monkeypatch.setattr(rag_router, "MAX_FILE_BYTES", 10)
    put("huge.txt", b"x" * 50)
    doc = rag_hub.post("/rag/index", json={"provider": "drive", "key": "huge.txt"}).json()["doc"]
    assert "too large" in doc["error"] and doc["size"] == 50

    docs = {d["key"]: d for d in rag_hub.get("/rag/docs").json()["docs"]}
    assert set(docs) == {"tool.exe", "huge.txt"}
    assert rag_hub.get("/rag/docs").json()["embedding_model"] == ""


def test_index_missing_file_and_bad_provider(rag_hub):
    assert rag_hub.post("/rag/index", json={"provider": "drive", "key": "nope.txt"}).status_code == 404
    assert rag_hub.post("/rag/index", json={"provider": "turso", "key": "a.txt"}).status_code == 400
    assert rag_hub.post("/rag/index", json={"provider": "ghost", "key": "a.txt"}).status_code == 404


def test_refuses_to_mix_embedding_models(rag_hub):
    put("a.txt", b"alpha document")
    put("b.txt", b"beta document")
    rag_hub.post("/rag/index", json={"provider": "drive", "key": "a.txt"})
    fakes_rag.EMBED["model"] = "other-embed"
    r = rag_hub.post("/rag/index", json={"provider": "drive", "key": "b.txt"})
    assert r.status_code == 409 and "re-index" in r.json()["detail"]
    fakes_rag.EMBED["model"] = "fake-embed"
    fakes_rag.EMBED["dim"] = 8
    assert rag_hub.post("/rag/index", json={"provider": "drive", "key": "b.txt"}).status_code == 409


def test_delete_and_model_reset(rag_hub):
    put("dir/a.txt", b"alpha document")
    rag_hub.post("/rag/index", json={"provider": "drive", "key": "dir/a.txt"})
    assert rag_hub.delete("/rag/docs/drive/dir/a.txt").status_code == 204
    assert rag_hub.get("/rag/docs").json() == {"docs": [], "embedding_model": ""}
    assert rag_hub.delete("/rag/docs/drive/dir/a.txt").status_code == 404
    # nothing indexed any more: a different embedding model is accepted
    fakes_rag.EMBED.update(model="other-embed", dim=8)
    put("b.txt", b"beta document")
    assert rag_hub.post("/rag/index", json={"provider": "drive", "key": "b.txt"}).json()["doc"]["chunks"] == 1
    assert rag_hub.get("/rag/docs").json()["embedding_model"] == "other-embed"


def test_delete_keeps_other_docs(rag_hub):
    put("a.txt", b"alpha document")
    put("b.txt", b"beta document")
    for key in ("a.txt", "b.txt"):
        rag_hub.post("/rag/index", json={"provider": "drive", "key": key})
    rag_hub.delete("/rag/docs/drive/a.txt")
    keys = [r[0] for r in fakes_rag.DBS["turso"].execute("select key from rag_chunks").fetchall()]
    assert keys == ["b.txt"]
    assert rag_hub.get("/rag/docs").json()["embedding_model"] == "fake-embed"


# --------------------------------------------------------------------------- ask


def _index_corpus(hub) -> None:
    put("fruit.pdf", make_pdf(["Apples are red and crunchy.", "Bananas are yellow and soft."]))
    put("cars.html", b"<html><body><p>Cars need fuel and tyres.</p></body></html>")
    for key in ("fruit.pdf", "cars.html"):
        assert hub.post("/rag/index", json={"provider": "drive", "key": key}).status_code == 200


def test_ask_with_citations(rag_hub):
    _index_corpus(rag_hub)
    r = rag_hub.post("/rag/ask", json={"question": "what colour are bananas", "top_k": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "Apples are red [1]."
    assert body["routed_via"] == "ragllm"
    assert [s["n"] for s in body["sources"]] == [1, 2]
    top = body["sources"][0]
    assert top["key"] == "fruit.pdf" and top["page"] == 2 and top["provider"] == "drive"
    assert "Bananas" in top["snippet"] and top["title"] == "fruit.pdf" and top["score"] > 0
    assert len(fakes_rag.RERANKS[-1].documents) == 3  # every retrieved chunk went to rerank
    prompt = fakes_rag.CHATS[-1].messages
    assert prompt[0].role == "system" and "[1]" in prompt[0].content and "not in the documents" in prompt[0].content
    assert "[1] fruit.pdf, page 2\nBananas are yellow" in prompt[1].content
    assert "Question: what colour are bananas" in prompt[1].content


def test_ask_falls_back_to_vector_order_without_rerank(make_hub):
    plugins = rag_plugins()
    plugins["ragllm"]["module"] = f"{R}:FakeNoRerankLLM"
    hub = make_hub(plugins)
    _index_corpus(hub)
    body = hub.post("/rag/ask", json={"question": "bananas yellow", "top_k": 1}).json()
    assert len(body["sources"]) == 1
    assert body["sources"][0]["page"] == 2
    assert 0 < body["sources"][0]["score"] <= 1


def test_ask_rerank_failure_falls_back(make_hub):
    hub = make_hub(rag_plugins(rerank_down=True))
    _index_corpus(hub)
    body = hub.post("/rag/ask", json={"question": "cars fuel"}).json()
    assert body["sources"][0]["key"] == "cars.html"
    assert len(body["sources"]) == 3


def test_ask_filters_by_keys(rag_hub):
    _index_corpus(rag_hub)
    body = rag_hub.post("/rag/ask", json={"question": "bananas", "keys": ["cars.html"]}).json()
    assert {s["key"] for s in body["sources"]} == {"cars.html"}


def test_ask_not_found(rag_hub):
    body = rag_hub.post("/rag/ask", json={"question": "anything?"}).json()
    assert body == {"answer": rag_router.NOT_FOUND_ANSWER, "sources": [], "routed_via": ""}
    _index_corpus(rag_hub)
    body = rag_hub.post("/rag/ask", json={"question": "bananas", "keys": ["missing.txt"]}).json()
    assert body["answer"] == rag_router.NOT_FOUND_ANSWER and body["sources"] == []
    assert not fakes_rag.CHATS  # no LLM call when nothing was retrieved


def test_ask_validation(rag_hub):
    assert rag_hub.post("/rag/ask", json={"question": ""}).status_code == 422
    assert rag_hub.post("/rag/ask", json={"question": "x", "top_k": 0}).status_code == 422


def test_rag_without_db_plugin(make_hub):
    plugins = rag_plugins()
    del plugins["turso"]
    hub = make_hub(plugins)
    assert hub.get("/rag/docs").status_code in (404, 409)


def test_citation_variants_are_normalised() -> None:
    from hub_api.routers.rag import normalise_citations

    assert normalise_citations("A【1】 B【2, 3】 C［4］ D[5]") == "A[1] B[2][3] C[4] D[5]"
