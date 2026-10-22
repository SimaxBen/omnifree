"""Linked notes: [[wiki links]] index, backlinks, title autocomplete, link targets (create on click), #tag filter."""

from __future__ import annotations

import pytest

from hub_api.note_links import body_tags, extract_links, snippet, title_key
from tests.hub_api import fakes_apps as fa


@pytest.fixture
def app_hub(make_hub):
    fa.reset()
    return make_hub(fa.apps_plugins())


def _note(hub, **body) -> dict:
    r = hub.post("/notes", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_extract_links_and_tags() -> None:
    body = (
        "See [[Project X]] and [[project   x|the project]] and [[Plan#Goals]].\n"
        "`[[Not a link]]`\n```\n[[Also not]]\n```\n[[ Spaced  title ]] [[]] [[a\nb]] #idea #Projet-x x#no #123 "
        "https://site.test/page#frag ## heading-ish"
    )
    assert extract_links(body) == ["Project X", "Plan", "Spaced title"]
    assert body_tags(body) == ["idea", "projet-x"]
    assert body_tags("# Heading\n#tag at start, (#paren) and #fin.") == ["tag", "paren", "fin"]
    assert title_key("  Project   X ") == "project x"
    assert snippet("Intro text. Then [[Project X]] is mentioned here.", "project x").startswith("Intro text. Then [[Project X]]")


def test_backlinks_follow_saves_and_deletes(app_hub) -> None:
    target = _note(app_hub, title="Project X", body="The project.")
    other = _note(app_hub, title="Unrelated", body="nothing")
    src = _note(app_hub, title="Daily", body="Worked on [[project x]] and [[Missing page]].\n`[[Unrelated]]`")

    r = app_hub.get(f"/notes/{target['id']}/backlinks")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["title"] == "Project X"
    assert [b["id"] for b in out["backlinks"]] == [src["id"]]
    assert "[[project x]]" in out["backlinks"][0]["snippet"] and out["backlinks"][0]["notebook"] == "Inbox"
    assert app_hub.get(f"/notes/{other['id']}/backlinks").json()["backlinks"] == []  # inside code: ignored

    outgoing = app_hub.get(f"/notes/{src['id']}/backlinks").json()["outgoing"]
    assert outgoing == [{"title": "project x", "note_id": target["id"]}, {"title": "Missing page", "note_id": None}]

    app_hub.patch(f"/notes/{src['id']}", json={"body": "no links any more"})
    assert app_hub.get(f"/notes/{target['id']}/backlinks").json()["backlinks"] == []
    app_hub.patch(f"/notes/{src['id']}", json={"body": "back to [[Project X]]"})
    assert len(app_hub.get(f"/notes/{target['id']}/backlinks").json()["backlinks"]) == 1
    app_hub.delete(f"/notes/{src['id']}")
    assert app_hub.get(f"/notes/{target['id']}/backlinks").json()["backlinks"] == []
    assert fa.sql("SELECT COUNT(*) FROM app_note_links")[0][0] == 0
    assert app_hub.get("/notes/nope/backlinks").status_code == 404


def test_index_backfills_notes_written_before_it(app_hub) -> None:
    target = _note(app_hub, title="Garden", body="plants")
    fa.sql(
        "INSERT INTO app_notes (id, title, body, notebook, tags, source, pinned, created_at, updated_at)"
        " VALUES ('old1', 'Old note', 'about the [[garden]]', 'Inbox', '[]', 'text', 0, '2025-01-01T00:00:00Z',"
        " '2025-01-01T00:00:00Z')"
    )
    back = app_hub.get(f"/notes/{target['id']}/backlinks").json()["backlinks"]
    assert [b["id"] for b in back] == ["old1"]


def test_titles_autocomplete_and_open_link(app_hub) -> None:
    for title in ("Meeting notes", "Team meeting", "Groceries"):
        _note(app_hub, title=title, body="x")
    titles = [t["title"] for t in app_hub.get("/notes/links/titles", params={"q": "meet"}).json()["titles"]]
    assert titles == ["Meeting notes", "Team meeting"]  # prefix match first
    assert len(app_hub.get("/notes/links/titles", params={"limit": 2}).json()["titles"]) == 2
    assert app_hub.get("/notes/links/titles", params={"q": "100%"}).json()["titles"] == []

    found = app_hub.post("/notes/links/open", json={"title": "  groceries "}).json()
    assert found["created"] is False and found["note"]["title"] == "Groceries"
    assert app_hub.post("/notes/links/open", json={"title": "New idea", "create": False}).status_code == 404
    made = app_hub.post("/notes/links/open", json={"title": "New   idea", "notebook": "Ideas"}).json()
    assert made["created"] is True and made["note"]["title"] == "New idea" and made["note"]["notebook"] == "Ideas"
    again = app_hub.post("/notes/links/open", json={"title": "new idea"}).json()
    assert again["created"] is False and again["note"]["id"] == made["note"]["id"]
    assert app_hub.post("/notes/links/open", json={"title": ""}).status_code == 422


def test_notes_tag_filter(app_hub) -> None:
    a = _note(app_hub, title="A", body="plain", tags=["idea"])
    b = _note(app_hub, title="B", body="this is an #idea in the text")
    _note(app_hub, title="C", body="many #ideas here")
    _note(app_hub, title="D", body="nothing")
    got = app_hub.get("/notes", params={"tag": "#Idea"}).json()["notes"]
    assert sorted(n["id"] for n in got) == sorted([a["id"], b["id"]])
    assert app_hub.get("/notes", params={"tag": "ideas"}).json()["notes"][0]["title"] == "C"


def test_titles_skip_untitled_notes(app_hub) -> None:
    _note(app_hub, title="Named", body="x")
    fa.sql(
        "INSERT INTO app_notes (id, title, body, notebook, tags, source, pinned, created_at, updated_at)"
        " VALUES ('blank1', '  ', 'no title', 'Inbox', '[]', 'text', 0, '2999-01-01T00:00:00Z', '2999-01-01T00:00:00Z')"
    )
    assert [t["title"] for t in app_hub.get("/notes/links/titles").json()["titles"]] == ["Named"]
    assert [t["title"] for t in app_hub.get("/notes/links/titles", params={"q": " "}).json()["titles"]] == ["Named"]


def test_link_index_writes_in_batches_and_rebuilds_in_pages(app_hub, monkeypatch: pytest.MonkeyPatch) -> None:
    from hub_api import note_links as nl

    body = " ".join(f"[[Page {i}]]" for i in range(nl.MAX_LINKS + 10))
    src = _note(app_hub, title="Hub", body=body)
    assert fa.sql("SELECT COUNT(*) FROM app_note_links WHERE src_id = ?", [src["id"]])[0][0] == nl.MAX_LINKS == 50

    app_hub.delete(f"/notes/{src['id']}")
    fa.sql("DELETE FROM app_note_links")
    fa.sql("DELETE FROM app_note_links_meta")
    for i in range(5):
        fa.sql(
            "INSERT INTO app_notes (id, title, body, notebook, tags, source, pinned, created_at, updated_at)"
            " VALUES (?, ?, '[[Target]]', 'Inbox', '[]', 'text', 0, '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')",
            [f"old{i}", f"Old {i}"],
        )
    target = _note(app_hub, title="Target", body="t")
    monkeypatch.setattr(nl, "INDEX_PAGE", 2)
    monkeypatch.setattr(nl, "INDEX_BUDGET_S", -1.0)  # one page per call, as if each call ran out of time
    seen = []
    for _ in range(6):
        seen.append(len(app_hub.get(f"/notes/{target['id']}/backlinks").json()["backlinks"]))
    assert seen[:3] == [2, 4, 5] and seen[-1] == 5  # resumes where it stopped, never restarts
    assert fa.sql("SELECT value FROM app_note_links_meta WHERE key = 'indexed'")[0][0] == "1"
    assert fa.sql("SELECT COUNT(*) FROM app_note_links_meta WHERE key = 'cursor'")[0][0] == 0


def test_notes_tag_filter_is_not_cut_short_by_false_matches(app_hub) -> None:
    for i in range(6):
        _note(app_hub, title=f"Noise {i}", body="lots of #ideas")
    real = _note(app_hub, title="Real", body="an #idea")
    fa.sql("UPDATE app_notes SET created_at = '2000-01-01T00:00:00Z' WHERE id = ?", [real["id"]])  # listed last
    got = app_hub.get("/notes", params={"tag": "idea", "limit": 3}).json()["notes"]
    assert [n["id"] for n in got] == [real["id"]]
    got = app_hub.get("/notes", params={"tag": "ideas", "q": "Noise 1", "limit": 3}).json()["notes"]
    assert [n["title"] for n in got] == ["Noise 1"]
