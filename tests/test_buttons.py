"""This week's buttons: Keep, Delete instead, Undo, and everything they must refuse."""

from conftest import press, publish, sample


def setup(env):
    _, db, client, film, _ = env
    assert publish(client, "film", sample("film-v1")).status_code == 200
    return db, client, film


def test_keep_adds_cleanup_keep_and_undo_removes_it(env):
    db, client, film = setup(env)
    r = press(client, 101, "keep")
    assert r.status_code == 303 and r.headers["location"] == "/#film-101"
    assert 101 in film.carrying("cleanup-keep")
    tap = db.one("SELECT * FROM taps")
    assert (tap["item_id"], tap["tag"], tap["page"], tap["added"]) == (
        101,
        "cleanup-keep",
        "week",
        1,
    )
    page = client.get("/").text.split('id="film-101"')[1].split("</li>")[0]
    assert "Kept. It will not be deleted." in page and 'value="undo"' in page
    assert press(client, 101, "undo").status_code == 303
    assert 101 not in film.carrying("cleanup-keep")
    assert db.one("SELECT undo_note FROM taps")["undo_note"] == "undone"
    # the writes were exactly: one editor add, one editor remove, of cleanup-keep (tag id 2)
    assert [(m, p, b["applyTags"]) for m, p, b in film.writes()] == [
        ("PUT", "/api/v3/movie/editor", "add"),
        ("PUT", "/api/v3/movie/editor", "remove"),
    ]
    assert all(b["tags"] == [2] and b["movieIds"] == [101] for _, _, b in film.writes())


def test_delete_instead_adds_cleanup_eligible_and_can_be_undone(env):
    _, client, film = setup(env)
    assert press(client, 105, "delete").status_code == 303
    assert 105 in film.carrying("cleanup-eligible")
    page = client.get("/").text.split('id="film-105"')[1].split("</li>")[0]
    assert "Marked for deletion at the next run." in page
    assert press(client, 105, "undo").status_code == 303
    assert 105 not in film.carrying("cleanup-eligible")


def test_a_missing_label_is_created_once_then_used(env):
    _, client, film = setup(env)
    del film.tags["cleanup-keep"]
    assert press(client, 101, "keep").status_code == 303
    assert ("POST", "/api/v3/tag", {"label": "cleanup-keep"}) in film.writes()
    assert 101 in film.carrying("cleanup-keep")


def test_refusals(env):
    _, client, film = setup(env)
    # an action the document does not offer for that id
    r = press(client, 105, "keep")
    assert r.status_code == 403 and "not something this list offers" in r.text
    r = press(client, 107, "delete")
    assert r.status_code == 403
    # an id not on the current list, or already deleted
    assert press(client, 999, "keep").status_code == 404
    assert press(client, 108, "keep").status_code == 404
    # undo with nothing of this app's to undo
    assert press(client, 101, "undo").status_code == 409
    # a second press before undoing the first
    assert press(client, 106, "keep").status_code == 303
    assert press(client, 106, "delete").status_code == 409
    # a form drawn from an older document
    token = client.get("/").text.split('name="token" value="')[1].split('"')[0]
    publish(client, "film", {**sample("film-v1"), "generated_at": "2026-11-22T08:00:00+00:00"})
    r = press(client, 101, "keep", token=token)
    assert r.status_code == 409 and "Reload" in r.text
    # a bad library, id, action or missing token
    assert (
        client.post("/act", data={"library": "music", "id": "1", "action": "keep"}).status_code
        == 400
    )
    assert press(client, 101, "nuke").status_code == 400
    assert press(client, 101, "keep", token="").status_code == 409
    assert (
        client.post("/act", data={"library": "film", "id": "1e3", "action": "keep"}).status_code
        == 400
    )
    # nothing beyond the one Keep on 106 was written
    puts = [b for m, _, b in film.writes() if m == "PUT"]
    assert puts == [{"movieIds": [106], "tags": [2], "applyTags": "add"}]


def test_cross_site_press_is_refused(env):
    _, client, film = setup(env)
    data = {"library": "film", "id": "101", "action": "keep", "token": "x"}
    assert (
        client.post("/act", data=data, headers={"origin": "https://evil.example"}).status_code
        == 403
    )
    assert (
        client.post("/act", data=data, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    )
    assert film.writes() == []


def test_tag_already_set_elsewhere_is_left_alone_and_never_undone(env):
    db, client, film = setup(env)
    film.tags["cleanup-keep"][1].add(101)
    assert press(client, 101, "keep").status_code == 303
    assert db.one("SELECT count(*) AS n FROM taps")["n"] == 0
    page = client.get("/").text.split('id="film-101"')[1].split("</li>")[0]
    assert "set outside this app" in page and "<form" not in page
    assert press(client, 101, "undo").status_code == 409
    assert 101 in film.carrying("cleanup-keep")


def test_undo_refused_once_the_tag_was_removed_elsewhere(env):
    db, client, film = setup(env)
    assert press(client, 101, "keep").status_code == 303
    film.tags["cleanup-keep"][1].discard(101)  # a person took it off in Radarr
    assert press(client, 101, "undo").status_code == 409
    assert db.one("SELECT undo_note FROM taps")["undo_note"] == "tag removed outside this app"


def test_a_tag_that_lands_late_is_confirmed_on_a_later_read(env):
    db, client, film = setup(env)
    film.lag = 2  # the first two read-backs still show the old tags
    assert press(client, 101, "keep").status_code == 303
    assert db.one("SELECT count(*) AS n FROM taps")["n"] == 1


def test_an_unconfirmed_add_is_pending_and_undoable_only_if_it_lands_soon(env):
    db, client, film = setup(env)
    film.ignore.add(101)
    r = press(client, 101, "keep")
    assert r.status_code == 502 and "did not land" in r.text
    tap = db.one("SELECT * FROM taps")
    assert (tap["tag"], tap["confirmed"], tap["undone_at"]) == ("cleanup-keep", 0, None)
    # while pending, no Undo and no second press
    card = client.get("/").text.split('id="film-101"')[1].split("</li>")[0]
    assert "waiting for it to show" in card and "<form" not in card
    assert press(client, 101, "undo").status_code == 409
    # it lands within the grace window: it was this app's change, so Undo may remove it
    film.ignore.clear()
    film.tags["cleanup-keep"][1].add(101)
    assert press(client, 101, "undo").status_code == 303
    assert 101 not in film.carrying("cleanup-keep")


def test_an_unconfirmed_add_never_claims_a_tag_that_appears_later(env):
    db, client, film = setup(env)
    film.ignore.add(101)
    assert press(client, 101, "keep").status_code == 502
    # the grace window passes with nothing landed, then somebody adds cleanup-keep in Radarr
    db.run("UPDATE taps SET at = '2026-01-01T00:00:00Z'")
    film.ignore.clear()
    film.tags["cleanup-keep"][1].add(101)
    assert press(client, 101, "undo").status_code == 409
    assert 101 in film.carrying("cleanup-keep")
    assert db.one("SELECT undo_note FROM taps")["undo_note"] == "never confirmed by the service"


def test_presses_and_publishes_share_one_lock():
    from curator import buttons, ingest

    assert buttons.LOCK is ingest.LOCK


def test_service_down_is_a_502_and_the_page_says_so(env):
    _, client, film = setup(env)
    film.fail = OSError("connection refused")
    assert press(client, 101, "keep", token="x").status_code == 409  # token checked first
    from conftest import token_of

    film.fail = None
    token = token_of(client, "film")
    film.fail = OSError("connection refused")
    assert press(client, 101, "keep", token=token).status_code == 502
    assert "The live tags could not be read." in client.get("/").text


def test_the_app_never_writes_the_keep_tag(env):
    import pytest
    from curator.arr import TagClient, radarr

    _, _, _, film, _ = env
    c = TagClient(radarr("http://radarr.test", "k"), opener=film)
    with pytest.raises(ValueError):
        c.add(101, "keep")
    with pytest.raises(ValueError):
        c.remove(301, "keep")
    assert film.writes() == []
