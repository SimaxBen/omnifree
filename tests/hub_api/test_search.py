"""GET /search: accent-insensitive global search across notes, tasks, money, Drive, chats and links."""

from __future__ import annotations

import pytest

from hub_api.routers.search import Field, fold, query_terms, score_item, snippet
from tests.hub_api import fakes_apps as fa


@pytest.fixture
def hub(make_hub):
    fa.reset()
    return make_hub(fa.apps_plugins())


def test_pure_helpers() -> None:
    assert fold("Été à FÈS ﬁn") == "ete a fes fin"
    assert query_terms("  Café, café  RÉSERVÉ!! ") == ["cafe", "reserve"]
    assert query_terms("!!!") == []
    title = [Field("Café Clock", 3, True), Field("long body", 1)]
    assert score_item(title, ["cafe"]) > score_item([Field("Le café", 3, True)], ["cafe"]) > 0
    assert score_item(title, ["cafe", "absent"]) == 0  # every word must match
    assert score_item([Field("x", 3, True), Field("un café", 1)], ["cafe"]) < score_item(title, ["cafe"])
    assert score_item([Field("cafe", 3, True)], ["cafe"]) > score_item([Field("cafe clock", 3, True)], ["cafe"])
    text = "Lorem ipsum " * 30 + "rendez-vous au Café de Fès demain" + " dolor" * 40
    snip = snippet(text, ["fes"])
    assert "Fès" in snip and snip.startswith("…") and snip.endswith("…") and len(snip) <= 145
    assert snippet("court", ["zzz"]) == "court"
    assert snippet("", ["a"]) == ""


def test_search_across_types(hub) -> None:
    note = hub.post("/notes", json={"title": "Recette tajine", "body": "Préparer le tajine au citron confit"}).json()
    hub.post("/notes", json={"title": "Liste", "body": "acheter du citron et du pain"})
    hub.post("/tasks", json={"title": "Réserver le café pour samedi"})
    hub.post("/tasks", json={"title": "Payer Inwi"})
    exp = hub.post("/expenses", json={"date": "2026-10-20", "merchant": "Café Clock", "total": 60,
                                      "category": "food", "note": "brunch"}).json()
    hub.post("/money/incomes", json={"date": "2026-10-01", "amount": 900, "source": "Freelance café site"})
    hub.post("/chats", json={"messages": [{"role": "user", "content": "Comment faire un tajine ?"},
                                          {"role": "assistant", "content": "Avec du citron confit."}]})
    hub.post("/links", json={"url": "https://example.org/tajine-recette", "title": "Tajine vidéo"})
    assert hub.post("/drive/upload", files={"file": ("tajine-photo.jpg", b"jpeg", "image/jpeg")},
                    data={"prefix": "photos"}).status_code == 201

    out = hub.get("/search", params={"q": "TAJINE"}).json()
    assert out["terms"] == ["tajine"] and not out["errors"]
    groups = {g["type"]: g for g in out["groups"]}
    assert set(groups) == {"notes", "chats", "links", "files"}
    assert groups["notes"]["items"][0]["id"] == note["id"]
    assert "tajine" in groups["notes"]["items"][0]["snippet"]
    assert groups["chats"]["items"][0]["snippet"].startswith("Comment faire un tajine")
    assert groups["links"]["items"][0]["data"] == {"kind": "link", "code": groups["links"]["items"][0]["data"]["code"],
                                                    "url": "https://example.org/tajine-recette"}
    f = groups["files"]["items"][0]
    assert f["title"] == "tajine-photo.jpg" and f["snippet"] == "photos" and f["data"]["key"] == "photos/tajine-photo.jpg"
    # "Tajine vidéo" starts with the word; "Recette tajine" only has it later in the title.
    assert groups["links"]["items"][0]["score"] > groups["notes"]["items"][0]["score"]
    assert out["groups"][0]["items"][0]["score"] >= max(g["items"][0]["score"] for g in out["groups"])

    out = hub.get("/search", params={"q": "cafe"}).json()  # accent-insensitive
    groups = {g["type"]: g for g in out["groups"]}
    assert set(groups) == {"tasks", "expenses", "incomes"}
    assert groups["expenses"]["items"][0]["data"]["id"] == exp["id"]
    assert groups["expenses"]["items"][0]["data"]["total"] == 60
    assert groups["tasks"]["items"][0]["data"]["done"] is False

    out = hub.get("/search", params={"q": "citron", "types": "notes", "limit": 1}).json()
    assert [g["type"] for g in out["groups"]] == ["notes"]
    assert out["groups"][0]["count"] == 2 and len(out["groups"][0]["items"]) == 1 and out["total"] == 2

    assert hub.get("/search", params={"q": "citron confit"}).json()["total"] == 2  # note + chat
    assert hub.get("/search", params={"q": "?!"}).json() == {"query": "?!", "terms": [], "total": 0, "groups": [],
                                                             "errors": {}}
    assert hub.get("/search", params={"q": "x", "types": "nope"}).status_code == 422
    assert hub.get("/search").status_code == 422


def test_search_on_empty_database_and_auth(hub) -> None:
    out = hub.get("/search", params={"q": "rien"}).json()
    assert out["total"] == 0 and out["groups"] == [] and out["errors"] == {}
    assert hub.client.get("/search", params={"q": "x"}).status_code == 401
