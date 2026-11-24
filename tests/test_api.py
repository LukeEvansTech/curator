"""The JSON API says what the pages say, and its one write is the pages' own press."""

from conftest import HOSTILE, publish, sample


def load(client):
    assert publish(client, "film", sample("film-v1")).status_code == 200
    assert publish(client, "tv", sample("tv-v2")).status_code == 200
    assert client.post("/allow/refresh").status_code == 200  # reads the IMDb ids


def week(client, library="film"):
    body = client.get("/api/v1/week").json()
    return next(lib for lib in body["libraries"] if lib["library"] == library)


def act(client, item_id, action, library="film", token=None):
    token = week(client, library)["token"] if token is None else token
    press = {"library": library, "id": item_id, "action": action, "token": token}
    return client.post("/api/v1/act", json=press)


def card(lib, item_id):
    return next(i for s in ("going", "needs_you", "later") for i in lib[s] if i["id"] == item_id)


def test_before_any_document_every_library_is_listed_and_empty(env):
    _, _, client, *_ = env
    body = client.get("/api/v1/week").json()
    assert [lib["library"] for lib in body["libraries"]] == ["film", "tv"]
    assert all(not lib["published"] and lib["going"] == [] for lib in body["libraries"])
    assert body["review_left"] == 0
    assert client.get("/api/v1/review").json()["entries"] == []
    assert client.get("/api/v1/deleted").json()["rows"] == []


def test_the_week_carries_the_page_headline(env):
    s, _, client, *_ = env
    load(client)
    body = client.get("/api/v1/week").json()
    assert body["version"] == s.version
    page = client.get("/").text
    for lib in body["libraries"]:
        section = page.split(f'<section id="{lib["library"]}">')[1].split("</section>")[0]
        assert f'<b class="count">{lib["going_count"]}</b>' in section
        assert f'on {lib["next_run_day"]}' in section
        assert f'{lib["going_size"]} · at most {lib["cap"]} a run' in section
        assert f'<b>{lib["counts"]["later"]}</b>later' in section
        for note in lib["notices"]:
            assert f"<b>{note['lead']}</b>" in section
    film = week(client)
    assert film["rehearsal"] is False and week(client, "tv")["rehearsal"] is True
    assert any(n["lead"] == "Dry run." for n in week(client, "tv")["notices"])


def test_a_title_reads_as_its_card_does(env):
    _, _, client, *_ = env
    load(client)
    gator = card(week(client), 101)
    assert gator["title"] == "Mega Gator vs. Lava Bear" and gator["year"] == 2023
    assert gator["section"] == "going" and gator["stand"] == "wait" and gator["goes"] is True
    assert gator["by"] == "Judge" and gator["reason"].startswith("Straight-to-video")
    assert gator["imdb_url"] == "https://www.imdb.com/title/tt0000101/"
    assert gator["poster"] == "/poster/film/101" and gator["page"] == "/title/film/101"
    assert gator["size"] in gator["facts"]
    assert [b["action"] for b in gator["buttons"]] == ["keep", "pass", "later"]
    assert [b["label"] for b in gator["buttons"]] == ["Keep", "Let it go", "Decide later"]


def test_keep_then_undo_moves_the_headline_and_the_tag(env):
    _, _, client, film, _ = env
    load(client)
    before = week(client)["going_count"]
    done = act(client, 101, "keep")
    assert done.status_code == 200 and done.json()["ok"] is True
    lib = done.json()["library"]
    assert 101 in film.carrying("cleanup-keep")
    assert lib["going_count"] == before - 1 and lib["kept_by_you"] == 1
    gator = card(lib, 101)
    assert gator["stand"] == "keep" and gator["goes"] is False and gator["tone"] == "kept"
    assert [b["action"] for b in gator["buttons"]] == ["undo"]
    assert "1 kept by you" in client.get("/").text  # the page agrees

    lib = act(client, 101, "undo").json()["library"]
    assert 101 not in film.carrying("cleanup-keep")
    assert lib["going_count"] == before and card(lib, 101)["stand"] == "wait"


def test_a_pass_and_a_put_off_change_no_tag_and_leave_the_title_going(env):
    _, _, client, film, _ = env
    load(client)
    left = client.get("/api/v1/week").json()["review_left"]
    done = act(client, 101, "pass").json()
    gator = card(done["library"], 101)
    assert gator["stand"] == "pass" and gator["goes"] is True
    assert [b["action"] for b in gator["buttons"]] == ["keep", "later", "unmark"]
    assert done["review_left"] == left - 1 and film.writes() == []

    gator = card(act(client, 101, "later").json()["library"], 101)
    assert gator["stand"] == "later" and gator["goes"] is True
    assert act(client, 101, "unmark").json()["review_left"] == left
    assert film.writes() == []


def test_review_lists_a_scope_in_page_order_with_its_tally(env):
    _, _, client, *_ = env
    load(client)
    body = client.get("/api/v1/review").json()
    assert body["scope"] == "due" and body["name"] == "Due at the next run"
    assert [s["scope"] for s in body["scopes"]] == ["due", "later", "kept"]
    assert body["total"] == len(body["entries"]) and body["answered"] == 0
    assert {e["section"] for e in body["entries"]} <= {"going", "needs_you"}
    assert set(body["tokens"]) == {"film", "tv"}
    assert body["rehearsal"] == ["tv"]  # the TV sample is a dry run, and the list says so
    assert any(n["lead"] == "Dry run." for n in body["notices"]["tv"])
    assert all(n["lead"] != "Dry run." for n in body["notices"]["film"])
    act(client, 101, "keep")
    body = client.get("/api/v1/review").json()
    assert body["keep"] == 1 and body["answered"] == 1
    later = client.get("/api/v1/review?scope=later").json()
    assert later["scope"] == "later" and {e["section"] for e in later["entries"]} == {"later"}
    assert client.get("/api/v1/review?scope=nonsense").json()["scope"] == "due"


def test_deleted_matches_the_page_and_says_when_it_is_cut(env):
    _, db, client, *_ = env
    load(client)
    body = client.get("/api/v1/deleted").json()
    page = client.get("/deleted").text
    assert body["rows"] and body["truncated"] is False and body["pages"] == 1
    assert f"{body['deleted_count']} deleted in all, {body['total_size']}" in page
    sizes = [r["size_gb"] or 0 for r in body["rows"]]
    assert sizes == sorted(sizes, reverse=True)  # largest first, as the page
    assert all(
        r["library"] == "tv" for r in client.get("/api/v1/deleted?library=tv").json()["rows"]
    )
    for n in range(150):
        db.run(
            "INSERT INTO actions (library, item_id, kind, title, size_gb, at, run_key)"
            " VALUES ('film', ?, 'deleted', ?, 1, '2026-11-01T08:00:00+00:00', 'run-x')",
            9000 + n,
            f"Filler {n}",
        )
    body = client.get("/api/v1/deleted").json()
    assert body["truncated"] is True and len(body["rows"]) == 100 and body["pages"] >= 2
    last = client.get("/api/v1/deleted?page=2").json()
    assert last["page"] == 2 and last["truncated"] is False  # nothing after the last page
    assert client.get("/api/v1/deleted?page=9").json()["page"] == 2


def test_a_press_is_refused_as_the_page_refuses_it(env):
    _, _, client, film, _ = env
    load(client)
    bad = act(client, 101, "keep", token="not-the-token")
    assert bad.status_code in (400, 403, 409) and bad.json()["ok"] is False and bad.json()["error"]
    assert act(client, 999, "keep").json()["ok"] is False  # not on the list
    assert act(client, 105, "keep").json()["ok"] is False  # the list offers no Keep for it
    assert act(client, 101, "keep", library="music", token="x").status_code == 400
    assert act(client, 101, "burn").status_code == 400
    assert client.post("/api/v1/act", json={"library": "film", "id": "101"}).status_code == 400
    assert client.post("/api/v1/act", json={"library": "film", "id": True}).status_code == 400
    for body in (b"", b"{not json", b"[1, 2]", b'"keep"'):
        r = client.post("/api/v1/act", content=body, headers={"content-type": "application/json"})
        assert r.status_code == 400 and r.json()["ok"] is False, body
    assert film.writes() == []
    # a page on another site cannot press through a browser
    token = week(client)["token"]
    press = {"library": "film", "id": 101, "action": "keep", "token": token}
    crossed = client.post("/api/v1/act", json=press, headers={"origin": "https://evil.example"})
    assert crossed.status_code == 403 and film.writes() == []


def test_a_second_keep_is_not_an_error(env):
    _, _, client, *_ = env
    load(client)
    assert act(client, 101, "keep").status_code == 200
    assert act(client, 101, "keep").status_code == 200


def test_a_service_that_does_not_answer_is_said_plainly(env):
    _, _, client, film, _ = env
    load(client)
    token = week(client)["token"]
    film.fail = OSError("connection refused")
    done = act(client, 101, "keep", token=token)
    assert done.status_code == 502 and done.json()["error"].startswith("Not done")
    lib = week(client)
    assert any(n["lead"] == "The live tags could not be read." for n in lib["notices"])


def test_hostile_text_travels_as_data(env):
    _, _, client, *_ = env
    doc = sample("film-v1")
    doc["going"][0]["title"] = HOSTILE
    doc["going"][0]["reason"] = HOSTILE
    assert publish(client, "film", doc).status_code == 200
    r = client.get("/api/v1/week")
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["x-content-type-options"] == "nosniff"
    first = week(client)["going"][0]
    assert first["title"] == HOSTILE.strip() and first["reason"] == HOSTILE.strip()
