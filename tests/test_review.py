"""One at a time: each press answers the title on screen and the next one is drawn."""

from conftest import HOSTILE, publish, sample

NEXT_WEEK = {"generated_at": "2026-11-22T08:00:00+00:00", "run_id": "run-20261122T080000-f1lm02"}


def setup(env):
    _, db, client, film, tv = env
    assert publish(client, "film", sample("film-v1")).status_code == 200
    assert publish(client, "tv", sample("tv-v2")).status_code == 200
    return db, client, film, tv


def url(scope: str = "") -> str:
    return f"/review?scope={scope}" if scope else "/review"


def fields(page: str) -> dict[str, str]:
    """What the answers under the title on screen send back with a press."""
    form = page.split('<form class="answers"')[1].split("</form>")[0]
    names = ("library", "id", "token", "scope")
    return {name: form.split(f'name="{name}" value="')[1].split('"')[0] for name in names}


def asked(client, scope: str = "") -> tuple[str, int] | None:
    """The title the page is asking about, or None when it asks about nothing."""
    page = client.get(url(scope)).text
    if '<form class="answers"' not in page:
        return None
    sent = fields(page)
    return sent["library"], int(sent["id"])


def press(client, action: str, scope: str = "", **over):
    sent = {**fields(client.get(url(scope)).text), "action": action, **over}
    return client.post("/review/act", data=sent, follow_redirects=False)


def labels(page: str) -> list[str]:
    form = page.split('<form class="answers"')[1].split("</form>")[0]
    return [part.split("</button>")[0].split(">")[-1] for part in form.split("<button")[1:]]


def test_it_asks_about_the_first_title_due_with_its_poster_and_buttons(env):
    _, client, *_ = setup(env)
    week = client.get("/").text
    strip = week.split('<nav class="jump"')[1].split("</nav>")[0]
    assert 'href="/review"' in strip and 'Review<b class="jump-n">6</b>' in strip
    r = client.get("/review")
    page = r.text
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert "<script" not in page
    assert asked(client) == ("film", 101)
    assert "Mega Gator vs. Lava Bear" in page and "Straight-to-video" in page
    assert 'src="/poster/film/101?w=500"' in page
    assert "<b>0</b> of 6 done" in page and 'max="6" value="0"' in page
    assert labels(page) == ["Keep", "Let it go", "Decide later"]
    # the next title's poster is fetched while this one is on screen
    assert 'class="ahead" src="/poster/film/102?w=500"' in page
    assert '<body class="focus">' in page  # the answers take the tabs' place on a phone


def test_keep_tags_the_title_and_draws_the_next(env):
    db, client, film, _ = setup(env)
    r = press(client, "keep")
    assert r.status_code == 303 and r.headers["location"] == "/review?last=film-101"
    assert 101 in film.carrying("cleanup-keep")
    tap = db.one("SELECT item_id, tag, page FROM taps")
    assert tuple(tap) == (101, "cleanup-keep", "week")  # the same press This week makes
    page = client.get(r.headers["location"]).text
    assert fields(page)["id"] == "102" and "<b>1</b> of 6 done" in page
    said = page.split('<form class="last"')[1].split("</form>")[0]
    assert '<b class="kept">Kept</b> Mega Gator vs. Lava Bear' in said and 'value="undo"' in said
    # and This week shows the same answer
    card = client.get("/").text.split('id="film-101"')[1].split("</li>")[0]
    assert "Kept. It will not be deleted." in card


def test_let_it_go_changes_no_tag_and_is_not_asked_twice(env):
    db, client, film, _ = setup(env)
    r = press(client, "pass")
    assert r.status_code == 303 and r.headers["location"] == "/review?last=film-101"
    assert film.writes() == [] and db.one("SELECT count(*) AS n FROM taps")["n"] == 0
    mark = db.one("SELECT item_id, mark, section FROM reviews")
    assert tuple(mark) == (101, "pass", "going")
    page = client.get(r.headers["location"]).text
    assert "<b>Left to go</b> Mega Gator vs. Lava Bear" in page and 'value="unmark"' in page
    assert asked(client) == ("film", 102)
    # next week's list still means to delete it, so the answer stands
    assert publish(client, "film", {**sample("film-v1"), **NEXT_WEEK}).status_code == 200
    assert asked(client) == ("film", 102)


def test_a_pass_is_asked_again_once_the_list_changes_its_mind(env):
    _, client, *_ = setup(env)
    assert asked(client, "kept") == ("film", 105)
    assert labels(client.get(url("kept")).text) == [
        "Fine for a year",
        "Delete instead",
        "Decide later",
    ]
    assert press(client, "pass", "kept").headers["location"] == "/review?scope=kept&last=film-105"
    assert asked(client, "kept") == ("tv", 202)
    # a year on the judge turns on it: agreeing to keep it was not agreeing to this
    doc = {**sample("film-v1"), **NEXT_WEEK}
    doc["going"] = [{**doc["kept"][0], "actions": ["keep"]}]
    doc["kept"] = []
    assert publish(client, "film", doc).status_code == 200
    assert asked(client) == ("film", 105)


def test_a_pass_answers_one_proposal_not_every_later_one(env):
    _, client, *_ = setup(env)
    assert press(client, "pass").status_code == 303 and asked(client) == ("film", 102)
    first = sample("film-v1")
    # the judge keeps it for a while, then proposes its deletion a second time
    kept = {**first, "generated_at": "2026-11-22T08:00:00+00:00", "run_id": "run-2"}
    kept["kept"] = first["kept"] + [{**first["going"][0], "actions": ["delete"]}]
    kept["going"] = first["going"][1:]
    assert publish(client, "film", kept).status_code == 200
    again = {**first, "generated_at": "2026-11-29T08:00:00+00:00", "run_id": "run-3"}
    again["going"] = [{**first["going"][0], "proposed_at": "2026-11-28T08:00:00+00:00"}]
    again["going"] += first["going"][1:]
    assert publish(client, "film", again).status_code == 200
    assert asked(client) == ("film", 101)


def test_a_keep_from_this_week_answers_over_a_pass_and_undo_asks_again(env):
    db, client, film, _ = setup(env)
    assert press(client, "pass").status_code == 303 and asked(client) == ("film", 102)
    token = fields(client.get("/review").text)["token"]
    sent = {"library": "film", "id": "101", "token": token}
    r = client.post("/act", data={**sent, "action": "keep"}, follow_redirects=False)
    assert r.status_code == 303 and 101 in film.carrying("cleanup-keep")
    assert db.one("SELECT count(*) AS n FROM reviews")["n"] == 0
    r = client.post("/act", data={**sent, "action": "undo"}, follow_redirects=False)
    assert r.status_code == 303 and asked(client) == ("film", 101)


def test_a_keep_the_service_refuses_leaves_the_earlier_answer_alone(env, monkeypatch):
    import io
    import urllib.error

    db, client, film, _ = setup(env)
    assert press(client, "pass").status_code == 303
    token = fields(client.get("/review").text)["token"]
    real_open = film.open

    def refuse_put(request, timeout=None):
        if request.get_method() == "PUT":
            raise urllib.error.HTTPError(request.full_url, 500, "boom", None, io.BytesIO())  # type: ignore[arg-type]
        return real_open(request, timeout)

    monkeypatch.setattr(film, "open", refuse_put)
    sent = {"library": "film", "id": "101", "action": "keep", "token": token}
    assert client.post("/act", data=sent).status_code == 502
    assert db.one("SELECT mark FROM reviews WHERE item_id = 101")["mark"] == "pass"
    assert asked(client) == ("film", 102)  # "Not done" left the title as it was: let go


def test_a_dry_run_says_so_on_every_title_it_asks_about(env):
    _, client, *_ = setup(env)
    assert "Dry run." not in client.get(url("kept")).text  # the film list acts
    assert press(client, "pass", "kept").status_code == 303
    page = client.get(url("kept")).text  # on to television, whose list is a rehearsal
    assert fields(page)["library"] == "tv"
    assert "<b>Dry run.</b> Nothing is deleted yet; the buttons still set real tags." in page


def test_decide_later_goes_to_the_end_and_comes_back(env):
    db, client, film, _ = setup(env)
    assert press(client, "later").headers["location"] == "/review?last=film-101"
    page = client.get("/review?last=film-101").text
    assert "<b>Put off</b> Mega Gator vs. Lava Bear" in page
    assert "<b>0</b> of 6 done · 1 put off" in page and fields(page)["id"] == "102"
    for _ in range(5):
        assert press(client, "pass").status_code == 303
    assert film.writes() == []
    done = client.get("/review").text
    assert asked(client) is None and '<body class="focus">' not in done
    assert "5 of 6 answered" in done and "Go through the 1 put off" in done
    assert "does not hold it back" in done
    assert 'Review<b class="jump-n">1</b>' in client.get("/").text  # still one to answer
    r = client.post("/review/again", data={"scope": "due"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/review"
    assert asked(client) == ("film", 101)
    # a put-off lasts for the list it was made on: the next list asks again without being told
    assert press(client, "later").status_code == 303 and asked(client) is None
    assert publish(client, "film", {**sample("film-v1"), **NEXT_WEEK}).status_code == 200
    assert asked(client) == ("film", 101)
    assert db.one("SELECT count(*) AS n FROM reviews WHERE mark = 'pass'")["n"] == 5


def test_undo_takes_the_last_answer_back(env):
    db, client, film, _ = setup(env)
    token = fields(client.get("/review").text)["token"]
    sent = {"library": "film", "id": "101", "token": token, "scope": "due"}
    assert press(client, "keep").status_code == 303
    for _ in range(2):  # the second press of Undo finds nothing left to take back
        r = client.post("/review/act", data={**sent, "action": "undo"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/review"
    assert 101 not in film.carrying("cleanup-keep") and asked(client) == ("film", 101)
    assert len(film.writes()) == 2
    assert press(client, "pass").status_code == 303 and asked(client) == ("film", 102)
    r = client.post("/review/act", data={**sent, "action": "unmark"}, follow_redirects=False)
    assert r.status_code == 303 and asked(client) == ("film", 101)
    assert db.one("SELECT count(*) AS n FROM reviews")["n"] == 0


def test_a_keep_the_service_never_shows_is_not_an_answer(env):
    db, client, film, _ = setup(env)
    film.ignore.add(101)  # Radarr takes the change and never applies it
    sent = {**fields(client.get("/review").text), "action": "keep"}
    r = client.post("/review/act", data=sent)
    assert r.status_code == 502 and "the tag did not land" in r.text
    assert 'href="/review">Back to the review</a>' in r.text
    # a second press does not pass for done while the first is still unconfirmed
    r = client.post("/review/act", data=sent)
    assert r.status_code == 409 and "Still waiting for the service" in r.text
    page = client.get("/review?last=film-101").text
    assert fields(page)["id"] == "102" and "<b>0</b> of 6 done · 1 not shown yet" in page
    assert "<b>Sent, not shown yet</b> Mega Gator vs. Lava Bear" in page
    for _ in range(5):
        assert press(client, "pass").status_code == 303
    done = client.get("/review").text
    assert "All 6 answered" not in done and "5 of 6 answered" in done
    assert "<b>1 not shown yet.</b>" in done and "<b>0</b>kept" in done
    assert 'Review<b class="jump-n">1</b>' in client.get("/").text
    # once the wait is over the press is given up, and the title is asked about again
    db.run("UPDATE taps SET at = '2026-01-01T00:00:00Z'")
    assert asked(client) == ("film", 101)


def test_a_tag_answers_over_an_earlier_pass(env):
    db, client, film, _ = setup(env)
    token = fields(client.get("/review").text)["token"]
    sent = {"library": "film", "id": "101", "token": token, "scope": "due"}
    assert press(client, "pass").status_code == 303
    r = client.post("/review/act", data={**sent, "action": "keep"}, follow_redirects=False)
    assert r.status_code == 303 and 101 in film.carrying("cleanup-keep")
    assert db.one("SELECT count(*) AS n FROM reviews")["n"] == 0


def test_a_second_press_of_the_same_button_is_harmless(env):
    _, client, film, _ = setup(env)
    sent = {**fields(client.get("/review").text), "action": "keep"}
    for _ in range(2):  # a double tap sends the same form twice
        r = client.post("/review/act", data=sent, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/review?last=film-101"
    assert len(film.writes()) == 1 and asked(client) == ("film", 102)
    sent = {**fields(client.get("/review").text), "action": "pass"}
    for _ in range(2):
        assert client.post("/review/act", data=sent, follow_redirects=False).status_code == 303
    assert asked(client) == ("film", 103)


def test_a_title_tagged_elsewhere_is_not_asked_about(env):
    _, client, film, _ = setup(env)
    film.tags["cleanup-keep"][1].add(101)  # kept in Radarr itself
    film.tags["cleanup-eligible"][1].add(102)  # and one marked for deletion there
    page = client.get("/review").text
    assert fields(page)["id"] == "103" and "<b>2</b> of 6 done" in page
    # said back if asked, but a tag this app did not add is not its to take off
    said = client.get("/review?last=film-101").text.split('<form class="last"')[1]
    assert "Kept</b>" in said.split("</form>")[0] and "<button" not in said.split("</form>")[0]


def test_refusals(env):
    _, client, film, _ = setup(env)
    sent = fields(client.get("/review").text)
    r = client.post("/review/act", data={**sent, "action": "keep", "token": "stale"})
    assert r.status_code == 409 and "The list has changed" in r.text
    assert 'href="/review">Back to the review</a>' in r.text
    r = client.post("/review/act", data={**sent, "action": "pass", "token": "stale"})
    assert r.status_code == 409
    assert client.post("/review/act", data={**sent, "action": "purge"}).status_code == 400
    assert (
        client.post("/review/act", data={**sent, "action": "pass", "id": "999"}).status_code == 404
    )
    assert (
        client.post("/review/act", data={**sent, "action": "unmark", "id": "999"}).status_code
        == 404
    )
    assert (
        client.post("/review/act", data={**sent, "action": "pass", "library": "music"}).status_code
        == 400
    )
    assert client.post("/review/act", data={**sent, "action": "pass", "id": "x"}).status_code == 400
    # the list does not offer Delete instead for a title that is going
    assert client.post("/review/act", data={**sent, "action": "delete"}).status_code == 403
    r = client.post(
        "/review/act", data={**sent, "action": "pass"}, headers={"origin": "https://elsewhere.test"}
    )
    assert r.status_code == 403
    assert film.writes() == [] and asked(client) == ("film", 101)
    assert client.get("/review?scope=nonsense&last=<b>-9").status_code == 200


def test_the_kept_list_offers_every_answer_in_the_same_places(env):
    _, _, client, film, _ = env
    doc = sample("film-v1")
    doc["kept"][0]["actions"] = ["keep", "delete"]
    assert publish(client, "film", doc).status_code == 200
    page = client.get(url("kept")).text
    assert labels(page) == ["Keep forever", "Delete instead", "Fine for a year", "Decide later"]
    assert '<span>Films</span> <span class="kept">Kept for a year</span>' in page
    r = press(client, "delete", "kept")
    assert r.headers["location"] == "/review?scope=kept&last=film-105"
    assert 105 in film.carrying("cleanup-eligible")
    said = client.get(r.headers["location"]).text.split('<form class="last"')[1]
    assert '<b class="gone">Marked for deletion</b>' in said.split("</form>")[0]
    assert 'name="scope" value="kept"' in said.split("</form>")[0]


def test_needs_you_and_later_titles_get_their_own_words(env):
    _, client, *_ = setup(env)
    for _ in range(3):
        assert press(client, "pass").status_code == 303
    page = client.get("/review").text
    assert (
        fields(page)["id"] == "106"
        and '<span>Films</span> <span class="needs-you">Needs you</span>' in page
    )
    assert labels(page) == ["Keep", "Delete instead", "Leave it", "Decide later"]
    assert press(client, "pass").status_code == 303
    assert labels(client.get("/review").text) == ["Keep", "Leave it", "Decide later"]
    later = client.get(url("later")).text
    assert (
        fields(later)["id"] == "104"
        and '<span>Films</span> <span class="later">Later</span>' in later
    )
    assert labels(later) == ["Keep", "Let it go", "Decide later"]


def test_the_end_says_what_was_answered_and_what_is_left(env):
    _, _, client, *_ = env
    empty = client.get("/review").text
    assert "Nothing to go through yet" in empty and '<form class="answers"' not in empty
    setup(env)
    assert press(client, "keep").status_code == 303
    for _ in range(5):
        assert press(client, "pass").status_code == 303
    done = client.get("/review").text
    assert "All 6 answered" in done and "<b>1</b>kept" in done and "<b>5</b>left as listed" in done
    assert 'href="/review?scope=later">Later</a><span class="num">1 left</span>' in done
    assert 'href="/review?scope=kept">Being kept for a year</a><span class="num">2 left' in done
    assert 'Review<b class="jump-n">0</b>' in client.get("/").text


def test_hostile_text_is_escaped(env):
    _, _, client, *_ = env
    doc = sample("film-v1")
    for item in doc["going"]:
        item.update(title=HOSTILE, reason=HOSTILE, overview=HOSTILE, requested_by=HOSTILE)
    assert publish(client, "film", doc).status_code == 200
    assert press(client, "pass").status_code == 303
    page = client.get("/review?last=film-101").text
    assert "<script>alert(1)" not in page and "<img src=x" not in page
    assert page.count("&lt;script&gt;") >= 4  # the answer said back, then the next title's text


def headline(client) -> tuple[int, str]:
    """The first library's big count on This week, and the line under it."""
    lead = client.get("/").text.split('<b class="count">')[1]
    return int(lead.split("</b>")[0]), lead.split('<p class="meta">')[1].split("</p>")[0]


def test_a_kept_title_leaves_the_headline_count(env):
    _, client, _, _ = setup(env)
    before, line = headline(client)
    assert before and "kept by you" not in line
    assert press(client, "keep").status_code == 303
    after, line = headline(client)
    assert after == before - 1 and "1 kept by you" in line
    assert press(client, "pass").status_code == 303  # letting one go changes nothing
    assert headline(client)[0] == after
