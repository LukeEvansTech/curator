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


def test_a_tag_seen_gone_closes_the_tap_so_a_later_one_is_not_undone(env):
    _, client, film = setup(env)
    assert press(client, 101, "keep").status_code == 303
    film.tags["cleanup-keep"][1].discard(101)  # removed in Radarr
    client.get("/")  # a page read observes the removal
    film.tags["cleanup-keep"][1].add(101)  # somebody adds it back in Radarr
    assert press(client, 101, "undo").status_code == 409
    assert 101 in film.carrying("cleanup-keep")


def test_a_failed_read_back_after_the_write_leaves_the_change_pending(env, monkeypatch):
    db, client, film = setup(env)
    from conftest import token_of
    from curator.arr import ArrError, TagClient

    token = token_of(client, "film")
    original = TagClient.tagged
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] > 1:  # the press's own read works; every read-back after the write fails
            raise ArrError("Radarr could not be reached")
        return original(self)

    monkeypatch.setattr(TagClient, "tagged", flaky)
    r = press(client, 101, "keep", token=token)
    monkeypatch.setattr(TagClient, "tagged", original)
    assert r.status_code == 502
    assert db.one("SELECT confirmed FROM taps")["confirmed"] == 0
    assert 101 in film.carrying("cleanup-keep")  # the write itself went through
    assert press(client, 101, "undo").status_code == 303  # seen within the grace window: ours


def test_live_keep_outranks_this_apps_delete_instead(env):
    _, client, film = setup(env)
    assert press(client, 105, "delete").status_code == 303
    film.tags["cleanup-keep"][1].add(105)
    card = client.get("/").text.split('id="film-105"')[1].split("</li>")[0]
    assert "which outranks this app" in card and "Marked for deletion" not in card
    assert 'value="undo"' in card


def test_a_page_drawn_while_a_press_holds_the_lock_never_closes_a_tap(env):
    from curator import buttons

    db, client, film = setup(env)
    assert press(client, 101, "keep").status_code == 303
    film.tags["cleanup-keep"][1].discard(101)  # what a read from before the press would show
    with buttons.LOCK:  # a press is mid-flight
        assert "Mega Gator" in client.get("/").text
    assert db.one("SELECT undone_at FROM taps")["undone_at"] is None
    film.tags["cleanup-keep"][1].add(101)  # the press's tag, as it really stands
    assert press(client, 101, "undo").status_code == 303


def test_an_unconfirmed_undo_gives_up_ownership(env):
    db, client, film = setup(env)
    assert press(client, 101, "keep").status_code == 303
    film.ignore.add(101)  # the removal is acknowledged but never shows
    assert press(client, 101, "undo").status_code == 502
    assert db.one("SELECT undo_note FROM taps")["undo_note"].startswith("undo sent")
    assert press(client, 101, "undo").status_code == 409  # whatever tag is there is not ours now


def test_a_lost_answer_to_the_write_is_read_back_not_abandoned(env, monkeypatch):
    db, client, film = setup(env)
    real_open = film.open

    def lose_put_reply(request, timeout=None):
        response = real_open(request, timeout)
        if request.get_method() == "PUT":
            raise TimeoutError("read timed out")  # applied, but the answer never arrived
        return response

    monkeypatch.setattr(film, "open", lose_put_reply)
    assert press(client, 105, "delete").status_code == 303  # read back: it landed
    assert 105 in film.carrying("cleanup-eligible")
    assert db.one("SELECT tag, confirmed FROM taps")["confirmed"] == 1
    assert press(client, 105, "undo").status_code == 303


def test_a_refused_write_is_a_plain_failure(env, monkeypatch):
    import io
    import urllib.error

    db, client, film = setup(env)
    real_open = film.open

    def refuse_put(request, timeout=None):
        if request.get_method() == "PUT":
            raise urllib.error.HTTPError(request.full_url, 500, "boom", None, io.BytesIO())  # type: ignore[arg-type]
        return real_open(request, timeout)

    monkeypatch.setattr(film, "open", refuse_put)
    assert press(client, 101, "keep").status_code == 502
    assert db.one("SELECT count(*) AS n FROM taps")["n"] == 0


def test_any_fresh_read_that_sees_the_tag_gone_ends_undo(env):
    from conftest import token_of

    db, client, film = setup(env)
    client.post("/allow/refresh")  # the title page reads live tags for titles in the library
    for observe in ("/allow", "/title/film/101", "refresh", "/ops", "press"):
        db.run("DELETE FROM taps")
        film.tags["cleanup-keep"][1].discard(101)
        assert press(client, 101, "keep").status_code == 303
        token = token_of(client, "film")  # a page already open, drawn while the tag was there
        film.tags["cleanup-keep"][1].discard(101)  # removed in Radarr
        if observe == "refresh":
            client.post("/allow/refresh")
        elif observe == "press":  # pressing another title reads every tag
            film.tags["cleanup-keep"][1].discard(103)
            db.run("DELETE FROM taps WHERE item_id = 103")
            assert press(client, 103, "keep", token=token).status_code == 303
        else:
            client.get(observe)
        film.tags["cleanup-keep"][1].add(101)  # added back in Radarr
        assert press(client, 101, "undo").status_code == 409, observe
        assert 101 in film.carrying("cleanup-keep")
