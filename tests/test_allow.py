"""The allow list: read from the services, why each title is on it, and cleanup-keep add and remove."""

from conftest import press, publish, sample
from curator import allow


def refreshed(env):
    _, db, client, film, tv = env
    assert client.post("/allow/refresh", follow_redirects=False).status_code == 303
    return db, client, film, tv


def test_reasons_follow_the_engines_rules():
    row = {"tags": [], "genres": [], "imdb_score": 6.5, "imdb_votes": 5000}
    assert allow.reasons("film", row) == ["bar"]
    assert allow.reasons("film", {**row, "imdb_votes": 4999}) == []
    assert allow.reasons("film", {**row, "imdb_score": 6.4}) == []
    assert allow.reasons("film", {**row, "imdb_score": None}) == []
    assert (
        allow.reasons("film", {**row, "tags": ["cleanup-eligible"]}) == []
    )  # switches the bar off
    assert allow.reasons("tv", {**row, "genres": ["Reality"]}) == []  # never for reality TV
    assert allow.reasons("film", {**row, "genres": ["Reality"]}) == ["bar"]
    assert allow.reasons("film", {**row, "tags": ["keep", "keep-review", "cleanup-keep"]}) == [
        "keep",
        "cleanup-keep",
        "keep-review",
        "bar",
    ]


def test_allow_list_shows_protected_titles_and_why(env):
    _, client, _, _ = refreshed(env)
    page = client.get("/allow").text
    assert "The Great Kettle Heist" in page  # keep tag and the bar
    assert "Fog Over Fenwick" in page  # cleanup-keep
    assert "The Slow Detective" in page  # TV, over the bar
    assert "Orbit of Small Hours" not in page  # over the bar but cleanup-eligible
    assert "Ninety Weddings in Ninety Days" not in page  # over the bar but Reality
    assert "Mega Gator" not in page
    assert "Mega Gator" in client.get("/allow?show=all&q=gator").text
    only = client.get("/allow?why=cleanup-keep").text
    assert "Fog Over Fenwick" in only and "Kettle" not in only
    assert "Kettle" not in client.get("/allow?library=tv").text
    # a search with LIKE wildcards matches them literally
    assert "Kettle" not in client.get("/allow?q=%25").text


def test_add_and_remove_cleanup_keep(env):
    db, client, film, _ = refreshed(env)
    back = "/allow?show=all&q=grandma"
    r = client.post(
        "/allow/act",
        data={"library": "film", "id": "303", "action": "add", "back": back},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == back
    assert 303 in film.carrying("cleanup-keep")
    assert "Grandma&#39;s Spaceship" in client.get("/allow").text
    r = client.post(
        "/allow/act",
        data={"library": "film", "id": "303", "action": "remove"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and 303 not in film.carrying("cleanup-keep")
    taps = db.all("SELECT action, added FROM taps ORDER BY id")
    assert [(t["action"], t["added"]) for t in taps] == [("allow-add", 1), ("allow-remove", 0)]
    # a cleanup-keep somebody set in Radarr can be removed here too, and nothing else is touched
    client.post("/allow/act", data={"library": "film", "id": "304", "action": "remove"})
    assert 304 not in film.carrying("cleanup-keep") and 301 in film.carrying("keep")


def test_allow_refusals(env):
    _, client, film, _ = refreshed(env)
    assert (
        client.post(
            "/allow/act", data={"library": "film", "id": "999", "action": "add"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/allow/act", data={"library": "film", "id": "301", "action": "keep"}
        ).status_code
        == 400
    )
    assert (
        client.post("/allow/act", data={"library": "x", "id": "301", "action": "add"}).status_code
        == 400
    )
    # an off-site redirect target is replaced by the allow list itself
    r = client.post(
        "/allow/act",
        data={"library": "film", "id": "303", "action": "add", "back": "//evil.example/allow"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/allow"
    assert all(b["tags"] == [2] for m, _, b in film.writes() if m == "PUT")  # only cleanup-keep


def test_removing_on_the_allow_list_closes_a_this_week_keep(env):
    _, db, client, _, _ = env
    publish(client, "film", sample("film-v1"))
    client.post("/allow/refresh")
    assert press(client, 101, "keep").status_code == 303
    client.post("/allow/act", data={"library": "film", "id": "101", "action": "remove"})
    assert (
        db.one("SELECT undo_note FROM taps WHERE page = 'week'")["undo_note"]
        == "removed on the allow list"
    )
    assert press(client, 101, "undo").status_code == 409


def test_a_failed_read_keeps_the_last_good_copy_and_says_so(env):
    db, client, film, _ = refreshed(env)
    film.fail = OSError("down")
    client.post("/allow/refresh")
    assert db.one("SELECT count(*) AS n FROM catalogue WHERE library = 'film'")["n"] == 6
    assert "failed: Radarr could not be reached" in client.get("/allow").text
    assert 'curator_allow_refresh_ok{library="film"} 0' in client.get("/metrics").text


def test_refresh_shares_the_lock_with_tag_changes():
    from curator import buttons

    assert allow.LOCK is buttons.LOCK


def test_a_pending_keep_confirmed_later_joins_the_allow_list(env):
    _, _, client, film, _ = env
    publish(client, "film", sample("film-v1"))
    client.post("/allow/refresh")
    film.ignore.add(101)
    assert press(client, 101, "keep").status_code == 502
    film.ignore.clear()
    film.tags["cleanup-keep"][1].add(101)  # it lands
    client.get("/")  # a page load confirms it
    assert "Mega Gator vs. Lava Bear" in client.get("/allow").text


def test_allow_list_shows_live_decision_tags_without_a_full_refresh(env):
    _, client, film, _ = refreshed(env)
    film.ignore.add(303)
    r = client.post("/allow/act", data={"library": "film", "id": "303", "action": "add"})
    assert r.status_code == 502  # taken, not seen
    assert "Grandma" not in client.get("/allow").text
    film.ignore.clear()
    film.tags["cleanup-keep"][1].add(303)  # it lands
    assert "Grandma" in client.get("/allow").text  # a reload shows it, no refresh needed
    film.tags["cleanup-keep"][1].discard(304)  # removed in Radarr
    assert "Fog Over Fenwick" not in client.get("/allow").text
