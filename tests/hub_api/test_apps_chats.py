"""/chats: saved AI conversations (fake D1 on SQLite, scripted LLM for the generated titles)."""

from __future__ import annotations

import pytest

from core.interfaces import ProviderUnavailable
from hub_api.routers.chats import chat_title, clean_llm_title
from tests.hub_api import fakes_apps as fa


@pytest.fixture(autouse=True)
def _reset_app_fakes() -> None:
    fa.reset()


@pytest.fixture
def app_hub(make_hub):
    return make_hub(fa.apps_plugins())


def _msgs(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"role": r, "content": c} for r, c in pairs]


# ------------------------------------------------------------------ pure helpers


def test_chat_title_and_clean_llm_title() -> None:
    assert chat_title(_msgs(("system", "be brief"), ("user", "  How do I\n\n *bake* bread?  "))) == "How do I bake bread?"
    assert chat_title(_msgs(("assistant", "hi"))) == "New chat"
    assert chat_title([]) == "New chat"
    long = chat_title(_msgs(("user", "word " * 40)))
    assert len(long) == 60 and long.endswith("…")
    assert clean_llm_title('Title: "Baking Sourdough Bread".') == "Baking Sourdough Bread"
    assert clean_llm_title("\n**Paris trip plan**\nextra line") == "Paris trip plan"
    assert clean_llm_title("   ") is None
    assert clean_llm_title(None) is None


# ------------------------------------------------------------------ CRUD


def test_create_get_list_patch_delete(app_hub) -> None:
    r = app_hub.post("/chats", json={"messages": _msgs(("user", "Plan a trip to Fes"), ("assistant", "Sure: day 1..."))})
    assert r.status_code == 201, r.text
    c1 = r.json()
    assert c1["title"] == "Plan a trip to Fes" and c1["title_source"] == "auto"
    assert c1["message_count"] == 2 and c1["preview"] == "Sure: day 1..."
    assert c1["pinned"] is False and c1["created_at"].endswith("Z") and len(c1["id"]) == 12
    assert c1["messages"][1] == {"role": "assistant", "content": "Sure: day 1..."}

    c2 = app_hub.post("/chats", json={"title": "Recipes", "model": "groq|g", "messages": _msgs(("user", "bread 100%"))}).json()
    assert (c2["title"], c2["title_source"], c2["model"]) == ("Recipes", "user", "groq|g")

    listing = app_hub.get("/chats").json()["chats"]
    assert {c["id"] for c in listing} == {c1["id"], c2["id"]}
    assert "messages" not in listing[0]
    assert app_hub.get(f"/chats/{c1['id']}").json() == c1

    # search covers the title and the message text; LIKE wildcards are escaped
    assert [c["id"] for c in app_hub.get("/chats", params={"q": "day 1"}).json()["chats"]] == [c1["id"]]
    assert [c["id"] for c in app_hub.get("/chats", params={"q": "recipes"}).json()["chats"]] == [c2["id"]]
    assert [c["id"] for c in app_hub.get("/chats", params={"q": "100%"}).json()["chats"]] == [c2["id"]]
    assert app_hub.get("/chats", params={"q": "_"}).json()["chats"] == []

    # append continues the conversation; the auto title follows the first user message
    r = app_hub.patch(f"/chats/{c1['id']}", json={"append": _msgs(("user", "and day 2?"), ("assistant", "Day 2: Meknes"))})
    assert r.status_code == 200
    p = r.json()
    assert p["message_count"] == 4 and p["messages"][-1]["content"] == "Day 2: Meknes"
    assert p["preview"] == "Day 2: Meknes" and p["title"] == "Plan a trip to Fes"
    assert p["created_at"] == c1["created_at"]

    # pin moves it first; rename marks the title as the user's; a blank title goes back to auto
    app_hub.patch(f"/chats/{c2['id']}", json={"pinned": True})
    assert app_hub.get("/chats").json()["chats"][0]["id"] == c2["id"]
    renamed = app_hub.patch(f"/chats/{c1['id']}", json={"title": "  Morocco trip "}).json()
    assert (renamed["title"], renamed["title_source"]) == ("Morocco trip", "user")
    back = app_hub.patch(f"/chats/{c1['id']}", json={"title": ""}).json()
    assert (back["title"], back["title_source"]) == ("Plan a trip to Fes", "auto")
    assert app_hub.patch(f"/chats/{c1['id']}", json={"pinned": None}).status_code == 422
    assert app_hub.patch("/chats/nope", json={"pinned": True}).status_code == 404

    assert app_hub.delete(f"/chats/{c1['id']}").status_code == 204
    assert app_hub.get(f"/chats/{c1['id']}").status_code == 404
    assert app_hub.delete(f"/chats/{c1['id']}").status_code == 404
    assert len(app_hub.get("/chats", params={"limit": 1}).json()["chats"]) == 1


def test_put_is_an_idempotent_upsert(app_hub) -> None:
    body = {"model": "auto", "messages": _msgs(("user", "first question"), ("assistant", "answer"))}
    r = app_hub.put("/chats/local-abc123", json=body)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["id"] == "local-abc123" and created["title"] == "first question"

    r = app_hub.put("/chats/local-abc123", json=body)  # a retry after a lost response
    assert r.status_code == 200
    assert r.json()["message_count"] == 2
    assert len(app_hub.get("/chats").json()["chats"]) == 1

    longer = {"messages": [*body["messages"], *_msgs(("user", "follow up"), ("assistant", "more"))]}
    again = app_hub.put("/chats/local-abc123", json=longer).json()
    assert again["message_count"] == 4 and again["model"] == "auto"  # an omitted model is kept
    assert again["created_at"] == created["created_at"]

    # a user title survives later saves that carry no title
    app_hub.patch("/chats/local-abc123", json={"title": "Mine"})
    assert app_hub.put("/chats/local-abc123", json=longer).json()["title"] == "Mine"

    assert app_hub.put("/chats/bad id!", json=body).status_code == 422
    assert app_hub.put("/chats/abc", json=body).status_code == 422  # too short


def test_validation_and_size_limit(app_hub) -> None:
    assert app_hub.post("/chats", json={"messages": [{"role": "tool", "content": "x"}]}).status_code == 422
    empty = app_hub.post("/chats", json={}).json()
    assert empty["title"] == "New chat" and empty["messages"] == [] and empty["preview"] == ""
    huge = _msgs(*[("user", "x" * 199_000)] * 10)
    r = app_hub.put("/chats/too-big-1", json={"messages": huge})
    assert r.status_code == 413
    assert "new chat" in r.text


def test_schema_created_once(app_hub) -> None:
    for _ in range(3):
        app_hub.get("/chats")
    assert len([s for s in fa.SQL_LOG if s.startswith("CREATE TABLE IF NOT EXISTS app_chats")]) == 1


# ------------------------------------------------------------------ generated titles


def test_auto_title_from_the_llm(app_hub) -> None:
    c = app_hub.post("/chats", json={"messages": _msgs(("user", "how do i make sourdough at home"), ("assistant", "Mix..."))}).json()
    fa.CHAT_SCRIPT["groq"].append('Title: "Homemade Sourdough Basics".')
    r = app_hub.post(f"/chats/{c['id']}/title")
    assert r.status_code == 200
    out = r.json()
    assert (out["title"], out["title_source"]) == ("Homemade Sourdough Basics", "llm")
    _, request = fa.CHAT_LOG[-1]
    assert request.max_tokens == 24
    assert "sourdough" in (request.messages[-1].content or "")

    # later saves keep the generated title
    saved = app_hub.put(f"/chats/{c['id']}", json={"messages": _msgs(("user", "other"), ("assistant", "x"))}).json()
    assert saved["title"] == "Homemade Sourdough Basics"


def test_auto_title_keeps_user_titles_and_survives_failures(app_hub) -> None:
    c = app_hub.post("/chats", json={"messages": _msgs(("user", "question one"))}).json()
    for name in ("groq", "vision_b", "gemini"):
        fa.CHAT_SCRIPT[name].append(ProviderUnavailable("down", provider=name))
    r = app_hub.post(f"/chats/{c['id']}/title")
    assert r.status_code == 200 and r.json()["title"] == "question one"

    fa.CHAT_SCRIPT["groq"].append("   ")
    assert app_hub.post(f"/chats/{c['id']}/title").json()["title_source"] == "auto"

    app_hub.patch(f"/chats/{c['id']}", json={"title": "My own"})
    calls = len(fa.CHAT_LOG)
    assert app_hub.post(f"/chats/{c['id']}/title").json()["title"] == "My own"
    assert len(fa.CHAT_LOG) == calls  # no LLM call for a renamed chat

    empty = app_hub.post("/chats", json={}).json()
    assert app_hub.post(f"/chats/{empty['id']}/title").json()["title"] == "New chat"
    assert app_hub.post("/chats/missing-1/title").status_code == 404
