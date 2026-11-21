"""Every page renders from a realistic document, and untrusted text never reaches the markup raw."""

from conftest import HOSTILE, press, publish, sample


def load(client):
    assert publish(client, "film", sample("film-v1")).status_code == 200
    assert publish(client, "tv", sample("tv-v2")).status_code == 200
    assert client.post("/allow/refresh").status_code == 200  # follows the redirect to /ops


def test_pages_render(env):
    _, _, client, *_ = env
    for path in ("/", "/runs", "/allow", "/deleted", "/ops"):
        assert client.get(path).status_code == 200, path  # before anything has arrived
    load(client)
    for path in (
        "/",
        "/runs",
        "/runs?library=tv",
        "/run/film/run-20261115T080000-f1lm01",
        "/run/tv/run-20261115T081500-tv0001",
        "/title/film/101",
        "/title/film/301",
        "/title/tv/202",
        "/allow",
        "/allow?show=all&q=gator",
        "/allow?why=bar&library=film",
        "/deleted",
        "/deleted?library=tv",
        "/ops",
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert client.get("/title/film/999").status_code == 404
    assert client.get("/run/film/nope").status_code == 404
    assert client.get("/title/music/1").status_code == 404
    assert client.get("/health").json()["ok"] is True


def test_this_week_shows_every_section(env):
    _, _, client, *_ = env
    load(client)
    page = client.get("/").text
    assert "Going on Sunday 22 November" in page and "Would go on Sunday 22 November" in page
    assert "Mega Gator vs. Lava Bear" in page and "Judge:</span> Straight-to-video" in page
    assert "You:</span> Marked for deletion by a person." in page
    assert "requested by housemate" in page
    assert "Needs you" in page and "Could not be assessed:</span>" in page
    assert "Later" in page and "Being kept" in page and "Deleted last run" in page
    assert "Dry run." in page and "The last run was blocked" in page
    # buttons offered only as each entry allows
    card = page.split('id="film-105"')[1].split("</li>")[0]
    assert 'value="delete"' in card and 'value="keep"' not in card
    card = page.split('id="film-107"')[1].split("</li>")[0]
    assert 'value="keep"' in card and 'value="delete"' not in card
    card = page.split('id="film-108"')[1].split("</li>")[0]
    assert "<form" not in card  # gone already: nothing to press


def test_run_page_shows_funnel_judge_cost_and_digest(env):
    _, _, client, *_ = env
    load(client)
    page = client.get("/run/tv/run-20261115T081500-tv0001").text
    assert "candidates" in page and "98" in page
    assert "184,000" in page and "21,500" in page and "claude-opus" in page
    # 0.184 M in at $4 + 0.0215 M out at $20 = $0.736 + $0.43
    assert "$1.17" in page
    assert "Bake or Bust: Lighthouse Edition" in page and "Open the review page" in page
    raw = client.get("/run/tv/run-20261115T081500-tv0001/document")
    assert raw.headers["content-type"].startswith("application/json")
    assert raw.json()["summary"]["judge"]["batches"] == 7
    v1 = client.get("/run/film/run-20261115T080000-f1lm01").text
    assert "no plan counts" in v1 and "no digest text" in v1


def test_title_page_has_verdicts_actions_taps_and_link(env):
    _, _, client, *_ = env
    load(client)
    page = client.get("/title/film/101").text
    assert "Straight-to-video creature filler" in page
    assert 'href="https://radarr.example/movie/slug-101"' in page
    assert press(client, 101, "keep").status_code == 303
    page = client.get("/title/film/101").text
    assert "Added cleanup-keep" in page
    assert "Deleted" in client.get("/title/film/108").text


def test_deleted_page_says_what_a_restore_needs(env):
    _, _, client, *_ = env
    load(client)
    page = client.get("/deleted").text
    assert "Shark Tornado 7: Final Spin" in page
    row = page.split("Shark Tornado 7")[1].split("</li>")[0]
    assert "In the recycle bin until" in row or "Past the recycle bin since" in row
    assert "Dry run: nothing was deleted." in page  # the TV run's would-delete


def test_ops_shows_documents_rejects_and_tags(env):
    _, _, client, *_ = env
    load(client)
    publish(client, "film", sample("film-v1"), key="wrong")
    page = client.get("/ops").text
    assert "cleanup-keep 1 · cleanup-eligible 1" in page
    assert "signature missing or wrong" in page
    assert "6 titles read" in page


def test_hostile_text_is_escaped_everywhere(env):
    _, _, client, *_ = env
    doc = sample("film-v1")
    for section in ("going", "kept", "needs_you", "deleted"):
        for item in doc[section]:
            item.update(title=HOSTILE, reason=HOSTILE, overview=HOSTILE, requested_by=HOSTILE)
    doc["blocks"] = [HOSTILE]
    doc["run_id"] = HOSTILE[:90]
    assert publish(client, "film", doc).status_code == 200
    tv = sample("tv-v2")
    tv["summary"]["digest"] = HOSTILE
    tv["summary"]["judge"]["model"] = HOSTILE
    tv["summary"]["plan"] = {"x": HOSTILE}
    assert publish(client, "tv", tv).status_code == 200
    for path in (
        "/",
        "/runs",
        "/run/film/doc-1",
        "/run/tv/run-20261115T081500-tv0001",
        "/title/film/101",
        "/deleted",
    ):
        page = client.get(path).text
        assert "<script>alert(1)" not in page, path
        assert "<img src=x" not in page, path
        if path != "/runs":  # the runs list shows none of the document's text
            assert "&lt;script&gt;" in page, path
