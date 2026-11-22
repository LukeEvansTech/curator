"""Posters come through this app and nowhere else, and IMDb is one tap from every title."""

import urllib.error

import pytest
from conftest import publish, sample
from curator import arr

JPEG = b"\xff\xd8\xff\xe0" + b"poster" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"poster" * 40
WEBP = b"RIFF" + b"\x00\x01\x00\x00" + b"WEBP" + b"poster" * 40


def asked(service) -> list[str]:
    """The poster paths a service was asked for, in order."""
    return [path for _, path, _ in service.requests if "/mediacover/" in path]


def test_the_types_a_page_may_show():
    assert arr.image_type(JPEG) == "image/jpeg"
    assert arr.image_type(PNG) == "image/png"
    assert arr.image_type(WEBP) == "image/webp"
    for other in (b"", b"<svg onload=alert(1)>", b"<!doctype html>", b"GIF89a", b"RIFF1234WAVE"):
        assert arr.image_type(other) == ""


def test_a_poster_is_fetched_once_then_served_from_the_volume(env):
    s, _, client, film, _ = env
    film.posters[101] = JPEG
    first = client.get("/poster/film/101")
    assert first.status_code == 200 and first.content == JPEG
    assert first.headers["content-type"] == "image/jpeg"
    assert first.headers["cache-control"] == "private, max-age=604800"
    assert first.headers["x-content-type-options"] == "nosniff"
    assert (s.data_dir / "posters" / "film" / "101-250").read_bytes() == JPEG
    assert client.get("/poster/film/101").content == JPEG
    assert asked(film) == ["/api/v3/mediacover/101/poster-250.jpg"]
    assert film.writes() == []


def test_a_kept_poster_outlives_the_title(env):
    _, _, client, film, _ = env
    film.posters[101] = JPEG
    client.get("/poster/film/101")
    del film.posters[101]  # deleted from the library since
    again = client.get("/poster/film/101")
    assert again.status_code == 200 and again.content == JPEG


def test_a_title_gone_from_the_service_shows_the_width_that_was_kept(env):
    _, _, client, film, _ = env
    film.posters[101] = JPEG
    client.get("/poster/film/101")  # the list's small one, while the film was there
    del film.posters[101]
    large = client.get("/poster/film/101?w=500", follow_redirects=False)
    assert large.status_code == 200 and large.content == JPEG
    assert large.headers["cache-control"] == "private, max-age=3600"


def test_a_poster_cut_short_on_the_way_is_not_kept(env):
    s, _, client, film, _ = env
    film.posters[101] = JPEG
    film.cut_short = True
    r = client.get("/poster/film/101", follow_redirects=False)
    assert r.status_code == 302 and r.headers["cache-control"] == "no-store"
    assert not (s.data_dir / "posters").exists()
    film.cut_short = False
    assert client.get("/poster/film/101").content == JPEG


def test_the_two_widths_are_kept_apart_and_no_other_is_asked_for(env):
    s, _, client, film, _ = env
    film.posters[101] = PNG
    assert client.get("/poster/film/101?w=500").headers["content-type"] == "image/png"
    assert client.get("/poster/film/101?w=9000").status_code == 200
    assert asked(film) == [
        "/api/v3/mediacover/101/poster-500.jpg",
        "/api/v3/mediacover/101/poster-250.jpg",
    ]
    assert sorted(p.name for p in (s.data_dir / "posters" / "film").iterdir()) == [
        "101-250",
        "101-500",
    ]


def test_a_title_with_no_poster_gets_the_placeholder_for_an_hour(env):
    s, _, client, _, tv = env
    r = client.get("/poster/tv/202", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/static/poster.svg"
    assert r.headers["cache-control"] == "private, max-age=3600"
    assert asked(tv) == ["/api/v3/mediacover/202/poster-250.jpg"]
    assert not (s.data_dir / "posters").exists()
    shown = client.get("/poster/tv/202")
    assert shown.status_code == 200 and shown.headers["content-type"].startswith("image/svg+xml")


def test_a_service_that_does_not_answer_is_asked_again_next_time(env):
    _, _, client, film, _ = env
    film.posters[101] = JPEG
    film.fail = urllib.error.URLError("down")
    r = client.get("/poster/film/101", follow_redirects=False)
    assert r.status_code == 302 and r.headers["cache-control"] == "no-store"
    film.fail = None
    assert client.get("/poster/film/101").content == JPEG


@pytest.mark.parametrize(
    "body",
    [
        b"<!doctype html><script>alert(1)</script>",
        b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>",
        JPEG + b"x" * arr.POSTER_LIMIT,
    ],
)
def test_what_is_not_a_small_image_is_neither_served_nor_kept(env, body):
    s, _, client, film, _ = env
    film.posters[101] = body
    r = client.get("/poster/film/101", follow_redirects=False)
    assert r.status_code == 302 and r.headers["cache-control"] == "no-store"
    assert not (s.data_dir / "posters").exists()


def test_only_a_known_library_and_a_real_id(env):
    _, _, client, film, _ = env
    for path in ("/poster/music/1", "/poster/film/0", "/poster/film/-4", "/poster/film/1e3"):
        assert client.get(path).status_code in (404, 422), path
    assert client.get("/poster/film/../../etc/passwd").status_code == 404
    assert asked(film) == []


def test_a_card_shows_its_poster_and_links_to_imdb(env):
    _, _, client, *_ = env
    assert publish(client, "film", sample("film-v1")).status_code == 200
    client.post("/allow/refresh")
    page = client.get("/").text
    card = page.split('id="film-101"')[1].split("</li>")[0]
    assert 'src="/poster/film/101"' in card and 'loading="lazy"' in card
    assert card.count('href="https://www.imdb.com/title/tt0000101/"') == 2  # poster and rating
    assert card.count('rel="noopener noreferrer"') == 2
    assert ">IMDb 3.1 on 412 votes</a>" in card
    # Nothing on the page comes from another address: the links are links, not loads.
    assert "image.tmdb.org" not in page and "<script" not in page
    assert client.get("/").headers["content-security-policy"].count("img-src 'self'") == 1


def test_a_title_with_no_known_id_links_to_an_imdb_search(env):
    _, _, client, *_ = env
    assert publish(client, "film", sample("film-v1")).status_code == 200
    client.post("/allow/refresh")
    card = client.get("/").text.split('id="film-105"')[1].split("</li>")[0]
    assert 'href="https://www.imdb.com/find/?q=Ten+Brides+in+a+Lighthouse+1984&amp;s=tt"' in card


def test_an_id_that_is_not_an_imdb_id_is_never_stored(env):
    _, db, client, film, _ = env
    film.titles[1]["imdb"] = "javascript:alert(1)"
    film.titles[2]["imdb"] = "tt12"
    client.post("/allow/refresh")
    assert [r["id"] for r in db.all("SELECT id FROM links WHERE library = 'film'")] == [101]


def test_the_imdb_id_is_kept_after_the_title_leaves_the_library(env):
    _, db, client, film, _ = env
    client.post("/allow/refresh")
    film.titles = [t for t in film.titles if t["id"] != 101]
    client.post("/allow/refresh")
    assert db.one("SELECT 1 FROM catalogue WHERE library = 'film' AND id = 101") is None
    assert db.one("SELECT imdb_id FROM links WHERE library = 'film' AND id = 101")["imdb_id"] == (
        "tt0000101"
    )


def test_the_title_page_and_the_lists_show_posters_too(env):
    _, _, client, *_ = env
    assert publish(client, "film", sample("film-v1")).status_code == 200
    client.post("/allow/refresh")
    title = client.get("/title/film/101").text
    assert 'src="/poster/film/101?w=500"' in title
    assert 'href="https://www.imdb.com/title/tt0000101/"' in title
    assert 'src="/poster/film/301"' in client.get("/allow").text
    deleted = client.get("/deleted").text
    assert 'src="/poster/film/' in deleted and "https://www.imdb.com/" in deleted
