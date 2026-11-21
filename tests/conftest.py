"""Shared fixtures: two in-memory services that speak the tag and library API, and an app over them."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from curator import config
from curator.arr import TagClient, radarr, sonarr
from curator.db import DB
from curator.document import sign
from curator.web import create_app
from fastapi.testclient import TestClient

DATA = Path(__file__).parent / "data"
KEYS = {"film": "radarr-key-0123456789abcdef", "tv": "sonarr-key-fedcba9876543210"}
# Text a hostile database entry or a model could hand over.
HOSTILE = '<script>alert(1)</script>"><img src=x onerror=alert(2)> & more'


class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeArr:
    """An in-memory Radarr or Sonarr that records every request."""

    def __init__(self, ids_field: str, tags: dict[str, list[int]] | None = None, titles=()):
        self.ids_field = ids_field
        film = ids_field == "movieIds"
        self.items_path = "/api/v3/movie" if film else "/api/v3/series"
        self.editor_path = f"{self.items_path}/editor"
        self.tags: dict[str, tuple[int, set[int]]] = {}
        for label, ids in (tags or {}).items():
            self.tags[label] = (len(self.tags) + 1, set(ids))
        self.titles = list(titles)
        self.requests: list[tuple[str, str, Any]] = []
        self.fail: Exception | None = None
        self.ignore: set[int] = set()  # ids whose tag the editor accepts and never applies
        self.lag = 0  # tag reads that still show the old state after an editor change
        self._stale = 0

    def carrying(self, label: str) -> set[int]:
        return set(self.tags.get(label, (0, set()))[1])

    def _record(self, item: dict) -> dict:
        tag_ids = [tid for _, (tid, ids) in self.tags.items() if item["id"] in ids]
        rating = {"value": item.get("score"), "votes": item.get("votes")}
        out = {
            "id": item["id"],
            "title": item["title"],
            "year": item.get("year"),
            "tags": tag_ids,
            "genres": item.get("genres", []),
            "titleSlug": item.get("slug", f"slug-{item['id']}"),
        }
        if self.ids_field == "movieIds":
            out.update(ratings={"imdb": rating}, sizeOnDisk=item.get("bytes", 5e9))
        else:
            out.update(ratings=rating, statistics={"sizeOnDisk": item.get("bytes", 5e9)})
        return out

    def open(self, request, timeout=None):  # pylint: disable=unused-argument
        path = "/" + request.full_url.split("/", 3)[3]
        body = json.loads(request.data) if request.data else None
        method = request.get_method()
        self.requests.append((method, path, body))
        if self.fail is not None:
            raise self.fail
        if method == "GET" and path == "/api/v3/tag/detail" and self._stale:
            self._stale -= 1
            return _Response(json.dumps(self._before).encode())
        if method == "GET" and path == "/api/v3/tag/detail":
            detail = [
                {"id": tid, "label": label, self.ids_field: sorted(ids)}
                for label, (tid, ids) in self.tags.items()
            ]
            return _Response(json.dumps(detail).encode())
        if method == "GET" and path == "/api/v3/tag":
            return _Response(
                json.dumps(
                    [{"id": tid, "label": label} for label, (tid, _) in self.tags.items()]
                ).encode()
            )
        if method == "GET" and path == self.items_path:
            return _Response(json.dumps([self._record(t) for t in self.titles]).encode())
        if method == "POST" and path == "/api/v3/tag":
            self.tags[body["label"]] = (len(self.tags) + 1, set())
            return _Response(
                json.dumps({"id": len(self.tags), "label": body["label"]}).encode(), 201
            )
        if method == "PUT" and path == self.editor_path:
            self._before = [
                {"id": tid, "label": label, self.ids_field: sorted(ids)}
                for label, (tid, ids) in self.tags.items()
            ]
            self._stale = self.lag
            for label, (tid, ids) in self.tags.items():
                if tid not in body["tags"]:
                    continue
                for item_id in body[self.ids_field]:
                    if item_id in self.ignore:
                        continue
                    if body["applyTags"] == "add":
                        ids.add(item_id)
                    elif body["applyTags"] == "remove":
                        ids.discard(item_id)
                self.tags[label] = (tid, ids)
            return _Response(b"[]", 202)
        raise urllib.error.HTTPError(request.full_url, 404, "not found", None, io.BytesIO())  # type: ignore[arg-type]

    def writes(self) -> list[tuple[str, str, Any]]:
        return [r for r in self.requests if r[0] != "GET"]


def sample(name: str) -> dict:
    return json.loads((DATA / f"{name}.json").read_text())


def body_of(payload: dict) -> bytes:
    """Serialised the way the engine's review.publish does."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def signed(library: str, payload: dict, key: str | None = None) -> tuple[bytes, dict[str, str]]:
    body = body_of(payload)
    return body, {
        "content-type": "application/json",
        "x-curator-signature": sign(key or KEYS[library], library, body),
    }


FILM_TITLES = [
    {"id": 101, "title": "Mega Gator vs. Lava Bear", "year": 2023, "score": 3.1, "votes": 412},
    {"id": 105, "title": "Ten Brides in a Lighthouse", "year": 1984, "score": 6.1, "votes": 4200},
    {"id": 301, "title": "The Great Kettle Heist", "year": 1999, "score": 7.4, "votes": 88000},
    {"id": 302, "title": "Orbit of Small Hours", "year": 2011, "score": 6.9, "votes": 12000},
    {"id": 303, "title": "Grandma's Spaceship", "year": 2005, "score": 5.0, "votes": 600},
    {"id": 304, "title": "Fog Over Fenwick", "year": 1978, "score": 6.2, "votes": 900},
]
TV_TITLES = [
    {
        "id": 201,
        "title": "Ninety Weddings in Ninety Days",
        "score": 7.0,
        "votes": 9000,
        "genres": ["Reality"],
    },
    {"id": 202, "title": "The Slow Detective", "score": 7.9, "votes": 24000, "genres": ["Drama"]},
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CURATOR_DATA", str(tmp_path))
    monkeypatch.setenv("RADARR_URL", "http://radarr.test")
    monkeypatch.setenv("RADARR_API_KEY", KEYS["film"])
    monkeypatch.setenv("SONARR_URL", "http://sonarr.test")
    monkeypatch.setenv("SONARR_API_KEY", KEYS["tv"])
    monkeypatch.setenv("RADARR_LINK_URL", "https://radarr.example")
    s = config.load()
    db = DB(s.db_path)
    film = FakeArr(
        "movieIds",
        {"keep": [301], "cleanup-keep": [304], "cleanup-eligible": [302], "keep-review": []},
        FILM_TITLES,
    )
    tv = FakeArr("seriesIds", {"keep": []}, TV_TITLES)
    clients = {
        "film": TagClient(radarr(s.radarr_url, s.radarr_key), opener=film, sleep=lambda _: None),
        "tv": TagClient(sonarr(s.sonarr_url, s.sonarr_key), opener=tv, sleep=lambda _: None),
    }
    client = TestClient(create_app(s, db, clients))
    return s, db, client, film, tv


def publish(client: TestClient, library: str, payload: dict, key: str | None = None):
    body, headers = signed(library, payload, key)
    return client.post(f"/publish/{library}", content=body, headers=headers)


def token_of(client: TestClient, library: str) -> str:
    """The form token This week draws for a library's buttons."""
    section = client.get("/").text.split(f'<section id="{library}">')[1].split("</section>")[0]
    return section.split('name="token" value="')[1].split('"')[0]


def press(
    client: TestClient, item_id: int, action: str, library: str = "film", token: str | None = None
):
    data = {"library": library, "id": str(item_id), "action": action}
    data["token"] = token_of(client, library) if token is None else token
    return client.post("/act", data=data, follow_redirects=False)
