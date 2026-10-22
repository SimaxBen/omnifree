"""/assistant: routing, the bounded tool loop over the MCP tools, confirmations, memory, attachments
(fake D1 on SQLite, scripted LLMs with and without function calling, fake storage; no network)."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from core.interfaces import ProviderUnavailable
from hub_api import assistant_core as core
from tests.hub_api import fakes_apps as fa
from tests.hub_api.fakes_assistant import assistant_plugins, calls


@pytest.fixture(autouse=True)
def _reset_app_fakes() -> None:
    fa.reset()


@pytest.fixture
def hub(make_hub):
    return make_hub(assistant_plugins())


@pytest.fixture
def plain_hub(make_hub):
    """No provider supports function calling: the JSON plan protocol runs instead."""
    return make_hub(assistant_plugins(tools=False))


def ask(hub, text: str, **extra: Any) -> Any:
    body = {"messages": [{"role": "user", "content": text}], **extra}
    return hub.post("/assistant/chat", json=body)


def requests_to(name: str) -> list[Any]:
    return [req for n, req in fa.CHAT_LOG if n == name]


def system_text(req: Any) -> str:
    return "\n".join(m.content for m in req.messages if m.role == "system" and isinstance(m.content, str))


# ------------------------------------------------------------------ pure helpers


@pytest.mark.parametrize(
    ("text", "route"),
    [
        ("Combien j'ai dépensé ce mois ?", "data"),
        ("how much did I spend on groceries in September?", "data"),
        ("chhal sreft had chhar?", "data"),
        ("شحال صرفت هاد الشهر؟", "data"),
        ("quelles sont mes tâches pour demain", "data"),
        ("J'ai un rendez-vous jeudi ?", "data"),
        ("what's on my agenda this week", "data"),
        ("ajoute une tâche: appeler le plombier demain à 10h", "data"),
        ("j'ai payé 120 dh chez Marjane", "data"),
        ("souviens-toi que mon salaire tombe le 25", "data"),
        ("je préfère le darija", "data"),
        ("cherche dans mes fichiers le contrat de bail", "data"),
        ("Translate 'good morning' to Spanish", "general"),
        ("write a python function to reverse a list", "general"),
        ("c'est quoi la photosynthèse ?", "general"),
        ("salut", "general"),
        ("thanks!", "general"),
        ("When did the Roman empire fall?", "general"),
        ("explain what a budget deficit is", "ambiguous"),
        ("what should I do this weekend?", "ambiguous"),
        ("est-ce que je suis libre samedi soir", "ambiguous"),
    ],
)
def test_classify(text: str, route: str) -> None:
    assert core.classify(text).route == route, core.classify(text)


def test_route_answer_language_and_folding() -> None:
    assert core.parse_route_answer(" DATA.") == "data"
    assert core.parse_route_answer("general") == "general"
    assert core.parse_route_answer("maybe") is None and core.parse_route_answer(None) is None
    assert core.detect_language("Tu as dépensé 120 MAD") == "fr"
    assert core.detect_language("You spent 120 MAD this month") == "en"
    assert core.detect_language("صرفت 120 درهم") == "ar"
    assert core.detect_language("wach 3ndi chi rendez-vous") == "fr"
    assert core.detect_language("") == "en"
    assert core.fold("  Élève\n Dépensé ’ ") == "eleve depense '"


def test_rank_memories_keywords_and_pinned() -> None:
    mems = [
        {"id": "1", "fact": "Mon salaire tombe le 25 du mois", "pinned": False, "created_at": "2026-01-01"},
        {"id": "2", "fact": "Je préfère les réponses en darija", "pinned": True, "created_at": "2026-01-02"},
        {"id": "3", "fact": "My car is a Dacia Logan", "pinned": False, "created_at": "2026-01-03"},
        {"id": "4", "fact": "Le salaire de ma femme tombe le 1er", "pinned": False, "created_at": "2026-02-01"},
    ]
    ranked = core.rank_memories("quand tombe mon salaire ?", mems)
    assert [m["id"] for m in ranked] == ["2", "4", "1"]  # pinned first, then overlap (tie: newest first)
    assert [m["id"] for m in core.rank_memories("hello", mems)] == ["2"]
    assert len(core.rank_memories("salaire tombe", mems, limit=1)) == 1
    assert core.keywords("Les dépenses d'électricité") == {"depens", "electr"}


def test_tool_helpers() -> None:
    spec = core.openai_tool("tasks_add", "Add a task", {"title": "x", "type": "object", "properties": {"t": {"title": "T", "type": "string"}}})
    assert spec == {"type": "function", "function": {"name": "tasks_add", "description": "Add a task",
                                                      "parameters": {"type": "object", "properties": {"t": {"type": "string"}}}}}
    assert core.parse_tool_args('{"a": 1}') == {"a": 1}
    assert core.parse_tool_args("not json") == {} and core.parse_tool_args("[1]") == {} and core.parse_tool_args({"b": 2}) == {"b": 2}
    plan = {"tools": [{"name": "tasks_list", "args": {}}, {"name": "drop_db"}, "x", {"tool": "expenses_month", "arguments": '{"month": "2026-10"}'},
                      {"name": "budget_status"}]}
    assert core.parse_plan(plan, core.READ_TOOLS, 2) == [("tasks_list", {}), ("expenses_month", {"month": "2026-10"})]
    assert core.parse_plan(None, core.READ_TOOLS, 4) == [] and core.parse_plan({"tools": "x"}, core.READ_TOOLS, 4) == []
    assert core.compact_json({"a": "x" * 50}, 30).endswith("…(truncated)")
    assert core.result_summary("tasks_list", {"tasks": [1, 2]}) == "2 tasks"
    assert core.result_summary("expenses_month", {"expenses": [1], "summary": {}, "month": "2026-10"}) == "1 expenses · 2026-10"
    assert core.result_summary("ask_drive", {"answer": "x", "sources": [1]}) == "1 source"
    assert core.result_summary("x", {"error": "boom"}) == "error: boom"
    assert core.write_summary("tasks_add", {"title": "Pain", "due": "demain", "due_time": "10:00", "priority": "high"}) == 'Task "Pain" · demain 10:00 · high priority'
    assert core.write_summary("expense_add", {"total": 120.5, "merchant": "Marjane", "category": "groceries"}) == "Expense 120.5 MAD · Marjane · groceries"
    assert core.write_summary("income_add", {"amount": 9000, "source": "Salaire", "recurring": True}) == "Income 9,000 MAD · Salaire · monthly"
    assert core.write_summary("notes_create", {"body": "# Idées\nblabla", "notebook": "Inbox"}) == 'Note "Idées" · Inbox'
    assert core.write_summary("calendar_add_event", {"title": "Dentiste", "start": "demain à 15h"}) == 'Event "Dentiste" · demain à 15h'
    assert core.write_summary("remember", {"fact": "x"}) == "Remember: x"


def test_pdf_context_budget() -> None:
    assert core.pdf_context([]) == ("", False)
    text, cut = core.pdf_context([(1, "hello"), (2, ""), (3, "world")])
    assert (text, cut) == ("[Page 1]\nhello\n\n[Page 3]\nworld", False)
    pages = [(i, f"page{i} " + "x" * 3000) for i in range(1, 11)]
    text, cut = core.pdf_context(pages, 8000)
    assert cut and len(text) <= 8200 and all(f"[Page {i}]" in text for i in range(1, 11))
    many = [(i, "y" * 1000) for i in range(1, 101)]
    text, cut = core.pdf_context(many, 4000)
    assert cut and "[Page 1]" in text and "[Page 100]" in text and "[Page 99]" in text and "omitted" in text


# ------------------------------------------------------------------ routing and the tool loop


def test_general_question_never_calls_tools_nor_sends_data(hub) -> None:
    hub.post("/assistant/memory", json={"fact": "Je préfère des réponses courtes", "pinned": True})
    hub.post("/assistant/memory", json={"fact": "Mon salaire tombe le 25"})
    fa.CHAT_SCRIPT["tooly"] = ["Buenos días"]
    r = ask(hub, "Translate 'good morning' to Spanish")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["routed"], body["content"], body["used_tools"], body["pending_actions"]) == ("general", "Buenos días", [], [])
    [req] = requests_to("tooly")
    assert req.tools is None
    sys = system_text(req)
    assert "réponses courtes" in sys and "salaire" not in sys and "tools over the owner's data" not in sys
    assert body["memories_used"] == 1 and body["routed_via"] == "tooly" and body["language"] == "en"


def test_data_question_runs_the_mcp_tool_and_answers(hub) -> None:
    hub.post("/expenses", json={"total": 120, "merchant": "Marjane", "category": "groceries"})
    hub.post("/assistant/memory", json={"fact": "Mes dépenses de courses sont chez Marjane"})
    fa.CHAT_SCRIPT["tooly"] = [calls(("expenses_month", {})), "Tu as dépensé 120 MAD ce mois-ci."]
    r = ask(hub, "Combien j'ai dépensé ce mois ?", chat_id="chat123456")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["routed"] == "data" and body["route_reason"].startswith("keyword:")
    assert body["content"] == "Tu as dépensé 120 MAD ce mois-ci." and body["language"] == "fr"
    [used] = body["used_tools"]
    assert used["name"] == "expenses_month" and used["ok"] and used["summary"].startswith("1 expenses")
    first, second = requests_to("tooly")
    names = {t["function"]["name"] for t in first.tools}
    assert {"expenses_month", "tasks_add", "remember", "calendar_events"} <= names
    assert not names & {"llm_chat", "drive_upload_text", "schedule_create", "links_create"}
    assert "Marjane" in system_text(first)  # the relevant memory is injected on data turns
    tool_msg = second.messages[-1]
    assert tool_msg.role == "tool" and tool_msg.tool_call_id == "call_0" and "Marjane" in tool_msg.content
    assert second.messages[-2].tool_calls[0]["function"]["name"] == "expenses_month"


def test_write_tools_need_confirmation(hub) -> None:
    fa.CHAT_SCRIPT["tooly"] = [
        calls(("tasks_add", {"title": "Acheter du pain", "due": "demain"})),
        "Je vais ajouter la tâche « Acheter du pain » pour demain : confirme.",
    ]
    body = ask(hub, "ajoute une tâche acheter du pain demain").json()
    [pending] = body["pending_actions"]
    assert pending["tool"] == "tasks_add" and pending["summary"] == 'Task "Acheter du pain" · demain'
    assert hub.get("/tasks").json()["tasks"] == []  # nothing written before the tap
    tool_msg = requests_to("tooly")[1].messages[-1]
    assert "pending_confirmation" in tool_msg.content

    r = hub.post("/assistant/confirm", json={"tool": pending["tool"], "args": pending["args"]})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] and r.json()["result"]["title"] == "Acheter du pain"
    [task] = hub.get("/tasks").json()["tasks"]
    assert task["title"] == "Acheter du pain" and task["due"]

    assert hub.post("/assistant/confirm", json={"tool": "tasks_list", "args": {}}).status_code == 422
    bad = hub.post("/assistant/confirm", json={"tool": "tasks_add", "args": {"title": ""}})
    assert bad.status_code == 422 and "title" in bad.json()["detail"]


def test_auto_confirm_writes_directly(hub) -> None:
    fa.CHAT_SCRIPT["tooly"] = [calls(("expense_add", {"total": 45, "merchant": "Café"})), "C'est noté : 45 MAD."]
    body = ask(hub, "j'ai payé 45 dh au café", auto_confirm=True).json()
    assert body["pending_actions"] == [] and body["used_tools"][0]["name"] == "expense_add"
    [exp] = hub.get("/expenses").json()["expenses"]
    assert exp["total"] == 45 and exp["merchant"] == "Café"


def test_remember_tool_saves_memory(hub) -> None:
    fa.CHAT_SCRIPT["tooly"] = [calls(("remember", {"fact": "Mon salaire tombe le 25"})), "Noté !"]
    body = ask(hub, "souviens-toi que mon salaire tombe le 25", chat_id="chatABCDEF").json()
    assert body["remembered"][0]["fact"] == "Mon salaire tombe le 25"
    assert body["used_tools"][0]["summary"] == "Remember: Mon salaire tombe le 25"
    [mem] = hub.get("/assistant/memory").json()["memories"]
    assert mem["source_chat"] == "chatABCDEF" and mem["pinned"] is False


def test_tool_call_limit_and_final_answer_without_tools(hub) -> None:
    loop = [calls(("tasks_list", {}), ("budget_status", {})) for _ in range(5)]
    fa.CHAT_SCRIPT["tooly"] = [*loop[:2], "Voilà."]
    body = ask(hub, "résume mes tâches et mon budget", mode="always").json()
    assert len(body["used_tools"]) == 4 and body["content"] == "Voilà."
    reqs = requests_to("tooly")
    assert len(reqs) == 3 and reqs[0].tools and reqs[2].tools is None


def test_extra_tool_calls_past_the_limit_are_refused(hub) -> None:
    fa.CHAT_SCRIPT["tooly"] = [calls(*[("tasks_list", {})] * 6), "Fini."]
    body = ask(hub, "mes tâches", mode="always").json()
    assert len(body["used_tools"]) == 4
    tool_msgs = [m for m in requests_to("tooly")[1].messages if m.role == "tool"]
    assert len(tool_msgs) == 6 and "limit reached" in tool_msgs[-1].content


def test_tool_errors_reach_the_model(hub) -> None:
    fa.CHAT_SCRIPT["tooly"] = [calls(("expenses_month", {"month": "octobre"})), "Mois invalide."]
    body = ask(hub, "mes dépenses d'octobre").json()
    [used] = body["used_tools"]
    assert used["ok"] is False and used["summary"].startswith("error")
    assert "YYYY-MM" in requests_to("tooly")[1].messages[-1].content


def test_ambiguous_message_asks_the_router_llm(hub) -> None:
    fa.CHAT_SCRIPT["tooly"] = ["GENERAL", "Go hiking in the Atlas."]
    body = ask(hub, "what should I do this weekend?").json()
    assert (body["routed"], body["route_reason"], body["used_tools"]) == ("general", "llm:general", [])
    route_req = requests_to("tooly")[0]
    assert route_req.max_tokens == 4 and route_req.tools is None

    fa.CHAT_LOG.clear()
    fa.CHAT_SCRIPT["tooly"] = ["DATA", calls(("calendar_events", {"days": 7})), "Rien de prévu."]
    body = ask(hub, "est-ce que je suis libre samedi soir").json()
    assert (body["routed"], body["route_reason"]) == ("data", "llm:data")
    assert body["used_tools"][0]["name"] == "calendar_events"

    fa.CHAT_LOG.clear()
    fa.CHAT_SCRIPT["tooly"] = [ProviderUnavailable("down", provider="tooly"), "fine"]
    fa.CHAT_SCRIPT["groq"] = [ProviderUnavailable("down", provider="groq")]
    fa.CHAT_SCRIPT["vision_b"] = [ProviderUnavailable("down", provider="vision_b")]
    fa.CHAT_SCRIPT["gemini"] = [ProviderUnavailable("down", provider="gemini")]
    body = ask(hub, "what should I do this weekend?").json()
    assert body["routed"] == "general" and body["route_reason"] == "llm-failed"


def test_modes_never_and_always(hub) -> None:
    hub.post("/assistant/memory", json={"fact": "Je préfère le darija", "pinned": True})
    fa.CHAT_SCRIPT["tooly"] = ["Je ne regarde pas tes données."]
    body = ask(hub, "combien j'ai dépensé ?", mode="never").json()
    assert body["routed"] == "general" and body["route_reason"] == "mode:never" and body["memories_used"] == 0
    [req] = requests_to("tooly")
    assert req.tools is None and "darija" not in system_text(req)

    fa.CHAT_LOG.clear()
    fa.CHAT_SCRIPT["tooly"] = ["Bonjour !"]
    body = ask(hub, "salut", mode="always").json()
    assert body["routed"] == "data" and requests_to("tooly")[0].tools


def test_plan_protocol_without_function_calling(plain_hub) -> None:
    plain_hub.post("/tasks", json={"title": "Payer la facture"})
    fa.CHAT_SCRIPT["groq"] = ['{"tools": [{"name": "tasks_list", "args": {}}, {"name": "rm_rf", "args": {}}]}',
                              "Tu as une tâche : payer la facture."]
    body = ask(plain_hub, "quelles sont mes tâches ?").json()
    assert body["content"] == "Tu as une tâche : payer la facture."
    assert [u["name"] for u in body["used_tools"]] == ["tasks_list"]
    plan_req, final_req = requests_to("groq")
    assert plan_req.tools is None and plan_req.response_format == {"type": "json_object"} and "tasks_list" in system_text(plan_req)
    assert final_req.tools is None and "Payer la facture" in system_text(final_req)


def test_failing_tool_provider_falls_back_to_plan(make_hub) -> None:
    plugins = assistant_plugins()
    plugins["tooly"]["settings"]["behaviour"] = "down"
    hub = make_hub(plugins)
    fa.CHAT_SCRIPT["groq"] = ['{"tools": [{"name": "tasks_add", "args": {"title": "Dentiste"}}]}', "Je crée « Dentiste » : confirme."]
    body = ask(hub, "ajoute une tâche dentiste").json()
    assert body["pending_actions"][0]["summary"] == 'Task "Dentiste"'
    assert "Task \"Dentiste\"" in system_text(requests_to("groq")[1])


def test_validation(hub) -> None:
    assert hub.post("/assistant/chat", json={"messages": []}).status_code == 422
    assert hub.post("/assistant/chat", json={"messages": [{"role": "assistant", "content": "hi"}]}).status_code == 422
    assert ask(hub, "x", mode="sometimes").status_code == 422


# ------------------------------------------------------------------ memory


def test_memory_crud_dedup_and_forget(hub) -> None:
    a = hub.post("/assistant/memory", json={"fact": "  Mon salaire   tombe le 25 "})
    assert a.status_code == 201 and a.json()["fact"] == "Mon salaire tombe le 25"
    dup = hub.post("/assistant/memory", json={"fact": "mon salaire tombe le 25"}).json()
    assert dup["id"] == a.json()["id"]
    b = hub.post("/assistant/memory", json={"fact": "Je préfère le darija", "pinned": True}).json()
    listing = hub.get("/assistant/memory").json()
    assert listing["count"] == 2 and listing["memories"][0]["id"] == b["id"]
    assert [m["id"] for m in hub.get("/assistant/memory", params={"q": "prefere"}).json()["memories"]] == [b["id"]]

    patched = hub.patch(f"/assistant/memory/{a.json()['id']}", json={"fact": "Mon salaire tombe le 28", "pinned": True}).json()
    assert patched["fact"] == "Mon salaire tombe le 28" and patched["pinned"] is True
    assert hub.patch("/assistant/memory/nope", json={"pinned": True}).status_code == 404
    assert hub.patch(f"/assistant/memory/{b['id']}", json={"fact": "   "}).status_code == 422
    assert hub.post("/assistant/memory", json={"fact": "x" * 301}).status_code == 422

    assert hub.delete(f"/assistant/memory/{b['id']}").status_code == 204
    assert hub.delete(f"/assistant/memory/{b['id']}").status_code == 404
    assert hub.delete("/assistant/memory").status_code == 422
    assert hub.delete("/assistant/memory", params={"confirm": "true"}).json() == {"deleted": 1}
    assert hub.get("/assistant/memory").json()["memories"] == []


# ------------------------------------------------------------------ attachments


def _pdf_with_text(text: str) -> bytes:
    """A one-page PDF with real text (Helvetica), built by hand with a correct xref table."""
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def _blank_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _upload(hub, key: str, data: bytes, ctype: str) -> dict[str, Any]:
    r = hub.post("/drive/upload", files={"file": (key.rsplit("/", 1)[-1], data, ctype)}, data={"key": key, "provider": "r2"})
    assert r.status_code == 201, r.text
    return {"provider": "r2", "key": key, "name": key.rsplit("/", 1)[-1], "content_type": ctype}


def test_pdf_attachment_is_read_as_page_marked_text(hub) -> None:
    att = _upload(hub, "chat/2026/10/facture.pdf", _pdf_with_text("Facture Lydec total 342 DH"), "application/pdf")
    fa.CHAT_SCRIPT["tooly"] = ["Le total est de 342 DH."]
    body = ask(hub, "Résume ce document", attachments=[att]).json()
    assert body["content"] == "Le total est de 342 DH."
    [rec] = body["attachments"]
    assert rec["mode"] == "text" and rec["pages"] == 1 and rec["key"] == "chat/2026/10/facture.pdf"
    user = requests_to("tooly")[-1].messages[-1]
    assert user.role == "user" and "[Page 1]" in user.content and "Lydec" in user.content and '<document name="facture.pdf"' in user.content


def test_image_and_scanned_pdf_go_to_the_vision_model(hub) -> None:
    img = _upload(hub, "chat/2026/10/photo.png", b"\x89PNG fake", "image/png")
    scan = _upload(hub, "chat/2026/10/scan.pdf", _blank_pdf(), "application/pdf")
    fa.CHAT_SCRIPT["gemini"] = ["Une photo et un scan."]
    body = ask(hub, "Qu'est-ce que c'est ?", attachments=[img, scan], mode="never").json()
    assert body["content"] == "Une photo et un scan." and [a["mode"] for a in body["attachments"]] == ["image", "native"]
    [req] = requests_to("gemini")
    parts = req.messages[-1].content
    assert parts[0] == {"type": "text", "text": "Qu'est-ce que c'est ?"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[2]["image_url"]["url"].startswith("data:application/pdf;base64,")
    assert requests_to("tooly") == []


def test_image_with_data_question_gathers_tools_then_vision(hub) -> None:
    hub.post("/expenses", json={"total": 80, "merchant": "Pharmacie", "category": "health"})
    img = _upload(hub, "chat/2026/10/ticket.jpg", b"\xff\xd8 fake", "image/jpeg")
    fa.CHAT_SCRIPT["tooly"] = [calls(("expenses_month", {})), "(discarded)"]
    fa.CHAT_SCRIPT["gemini"] = ["Ce ticket est déjà dans tes dépenses."]
    body = ask(hub, "est-ce que cette dépense est déjà enregistrée ?", attachments=[img]).json()
    assert body["content"] == "Ce ticket est déjà dans tes dépenses." and body["used_tools"][0]["name"] == "expenses_month"
    [req] = requests_to("gemini")
    assert "Pharmacie" in system_text(req)


def test_attachment_errors(hub) -> None:
    missing = {"provider": "r2", "key": "chat/2026/10/none.png", "content_type": "image/png"}
    assert ask(hub, "x", attachments=[missing]).status_code == 404
    zip_att = _upload(hub, "chat/2026/10/a.zip", b"PK", "application/zip")
    assert ask(hub, "x", attachments=[zip_att]).status_code == 415
    assert ask(hub, "x", attachments=[missing] * 5).status_code == 422


def test_saved_chat_keeps_attachments_and_tools(hub) -> None:
    msgs = [
        {"role": "user", "content": "Résume", "attachments": [{"provider": "r2", "key": "chat/2026/10/a.pdf", "name": "a.pdf"}]},
        {"role": "assistant", "content": "Ok", "tools": [{"name": "tasks_list", "summary": "2 tasks"}]},
    ]
    saved = hub.put("/chats/chat-att-1", json={"messages": msgs}).json()
    assert saved["messages"][0]["attachments"][0]["key"] == "chat/2026/10/a.pdf"
    assert saved["messages"][1]["tools"] == [{"name": "tasks_list", "summary": "2 tasks"}]
    assert json.loads(json.dumps(saved))  # plain JSON
