"""The only code that talks to Radarr or Sonarr, and all it can say.

Reads: which titles carry the two decision tags, the whole library with its tags (for the allow
list), and one title's poster. Writes: create one of the two decision labels if the service has
never seen it, and add or remove one of the two on one title. The label is checked here, at the last step before the network,
so no caller can widen what this app is able to write.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

TAG_KEEP = "cleanup-keep"
TAG_DELETE = "cleanup-eligible"
TAGS = (TAG_KEEP, TAG_DELETE)  # the only labels this app may add or remove
# Read for the allow list, never written: `keep` is the engines' own, `keep-review` is legacy.
ALLOW_TAGS = ("keep", TAG_KEEP, "keep-review")
# An IMDb title id, as it goes into a link.
IMDB_ID = re.compile(r"tt\d{5,12}")
# The poster widths both services resize to, smallest first, and the most one may weigh.
POSTER_WIDTHS = (250, 500)
POSTER_LIMIT = 2 * 1024 * 1024


class ArrError(RuntimeError):
    """The service could not be read, or a tag change could not be confirmed."""


class Refusal(ArrError):
    """The service answered with an error status: a definite no, nothing was changed."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class Unconfirmed(ArrError):
    """The service took the change, but the tag had not moved by the last read-back."""


@dataclass(frozen=True)
class Service:
    """Where one library lives, and what its API calls its titles."""

    name: str
    base_url: str
    api_key: str
    items_path: str
    editor_path: str
    ids_field: str


def radarr(base_url: str, api_key: str) -> Service:
    return Service(
        "Radarr", base_url.rstrip("/"), api_key, "/api/v3/movie", "/api/v3/movie/editor", "movieIds"
    )


def sonarr(base_url: str, api_key: str) -> Service:
    return Service(
        "Sonarr",
        base_url.rstrip("/"),
        api_key,
        "/api/v3/series",
        "/api/v3/series/editor",
        "seriesIds",
    )


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def image_type(head: bytes) -> str:
    """The media type of an image by its first bytes, or "" when it is not one a page may show."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return ""


def title_of(record: dict, labels: dict[int, str], film: bool) -> dict | None:
    """One library record reduced to what the allow list shows, or None when it has no id."""
    item_id = _int(record.get("id"))
    if not item_id:
        return None
    ratings = record.get("ratings") or {}
    if film:
        ratings = ratings.get("imdb") or {}
        size = record.get("sizeOnDisk")
    else:
        size = (record.get("statistics") or {}).get("sizeOnDisk")
    score, votes = _num(ratings.get("value")), _int(ratings.get("votes"))
    imdb = str(record.get("imdbId") or "")
    return {
        "id": item_id,
        "title": str(record.get("title") or f"#{item_id}")[:300],
        "year": _int(record.get("year")),
        "size_gb": round((_num(size) or 0) / 1e9, 2),
        "imdb_score": score or None,
        "imdb_votes": votes or None,
        "genres": [str(g)[:40] for g in record.get("genres") or [] if isinstance(g, str)][:20],
        "tags": sorted({labels[t] for t in record.get("tags") or [] if t in labels}),
        "slug": str(record.get("titleSlug") or "")[:200],
        "imdb_id": imdb if IMDB_ID.fullmatch(imdb) else "",
    }


class TagClient:
    """Reads one library, and changes the two decision tags in it."""

    # Read-backs after a tag change, and the pause before each re-read.
    CONFIRM_READS = 5
    CONFIRM_PAUSE = 0.6

    def __init__(self, service: Service, opener=None, timeout: int = 20, sleep=time.sleep):
        self.service = service
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener()
        self._sleep = sleep

    @property
    def configured(self) -> bool:
        return bool(self.service.base_url and self.service.api_key)

    def _open(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        method: str,
        path: str,
        body: dict | None = None,
        timeout: int = 0,
        accept: str = "application/json",
        limit: int = 0,
    ) -> bytes:
        """One request; the raw body, or ArrError on any failure. `limit` reads one byte past
        it and no further, so the caller can tell a body that is too large."""
        if not self.configured:
            raise ArrError(f"{self.service.name} is not configured")
        headers = {"X-Api-Key": self.service.api_key, "Accept": accept}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.service.base_url}{path}", data=payload, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise Refusal(
                        f"{self.service.name} answered {response.status} to {method}",
                        response.status,
                    )
                return response.read(limit + 1) if limit else response.read()
        except urllib.error.HTTPError as exc:
            exc.close()  # the error holds the response open and nothing reads its body
            raise Refusal(f"{self.service.name} answered {exc.code} to {method}", exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            # HTTPException covers a truncated reply (IncompleteRead): an answer lost in transit
            raise ArrError(f"{self.service.name} could not be reached") from exc

    def _send(self, method: str, path: str, body: dict | None = None, timeout: int = 0) -> Any:
        """One request; the decoded body, or ArrError on any failure."""
        raw = self._open(method, path, body, timeout)
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError as exc:
            raise ArrError(f"{self.service.name} sent a body that is not JSON") from exc

    def _details(self) -> dict[str, dict]:
        """The service's record of the two decision tags, by label; an unseen label is absent."""
        data = self._send("GET", "/api/v3/tag/detail")
        if not isinstance(data, list):
            raise ArrError(f"{self.service.name} did not return a list of tags")
        return {t["label"]: t for t in data if isinstance(t, dict) and t.get("label") in TAGS}

    def tagged(self) -> dict[str, frozenset[int]]:
        """Which titles carry each of the two decision tags right now."""
        details = self._details()
        return {
            label: frozenset(
                i
                for i in (details.get(label) or {}).get(self.service.ids_field) or []
                if _int(i) is not None
            )
            for label in TAGS
        }

    def library(self) -> list[dict]:
        """Every title in the library with its tag labels, for the allow list."""
        tags = self._send("GET", "/api/v3/tag")
        if not isinstance(tags, list):
            raise ArrError(f"{self.service.name} did not return a list of tags")
        labels = {
            t["id"]: str(t.get("label"))
            for t in tags
            if isinstance(t, dict) and _int(t.get("id")) is not None
        }
        records = self._send("GET", self.service.items_path, timeout=120)
        if not isinstance(records, list):
            raise ArrError(f"{self.service.name} did not return a list of titles")
        film = self.service.ids_field == "movieIds"
        out = [title_of(r, labels, film) for r in records if isinstance(r, dict)]
        return [t for t in out if t]

    def poster(self, item_id: int, width: int) -> tuple[bytes, str] | None:
        """One title's poster at one of the widths the service resizes to, with its media type;
        None when the service has no poster for it.

        The bytes go to a browser from this app's own address, so they are held to a size and
        to the first bytes of an image; the service's own word for the type is not used.
        """
        if width not in POSTER_WIDTHS:
            raise ValueError(f"no poster is {width} wide")
        path = f"/api/v3/mediacover/{int(item_id)}/poster-{width}.jpg"
        try:
            raw = self._open("GET", path, accept="image/*", limit=POSTER_LIMIT)
        except Refusal as exc:
            if exc.status == 404:
                return None
            raise
        kind = image_type(raw[:16])
        if len(raw) > POSTER_LIMIT or not kind:
            raise ArrError(f"{self.service.name} sent a poster that cannot be shown")
        return raw, kind

    def _tag_id(self, label: str) -> int:
        """The id of one of the two decision tags, creating the label when the service lacks it."""
        tag = self._details().get(label)
        if tag is None:
            self._send("POST", "/api/v3/tag", {"label": label})
            tag = self._details().get(label)
        tag_id = _int((tag or {}).get("id"))
        if tag_id is None:
            raise ArrError(f"{self.service.name} has no usable id for the tag {label}")
        return tag_id

    def _apply(self, item_id: int, label: str, mode: str) -> None:
        """Add or remove one tag on one title, then confirm it by reading the tags back.

        The editor endpoint acknowledges before it has done anything, so its answer is not
        evidence that the tag changed.
        """
        if label not in TAGS:
            raise ValueError(f"{label!r} is not a tag this app may change")
        if mode not in ("add", "remove"):
            raise ValueError(f"{mode!r} is not a tag change")
        if _int(item_id) is None or item_id <= 0:
            raise ValueError(f"{item_id!r} is not a title id")
        tag_id = self._tag_id(label)
        try:
            self._send(
                "PUT",
                self.service.editor_path,
                {self.service.ids_field: [item_id], "tags": [tag_id], "applyTags": mode},
            )
        except Refusal:
            raise
        except ArrError:
            pass  # a lost or garbled answer: the change may have been made, so read back
        # From here the change may have been made, so any failure to see it is "unconfirmed",
        # never a plain error: the caller must treat the change as possibly made.
        for attempt in range(self.CONFIRM_READS):
            if attempt:
                self._sleep(self.CONFIRM_PAUSE)
            try:
                if (item_id in self.tagged()[label]) == (mode == "add"):
                    return
            except ArrError:
                continue
        raise Unconfirmed(
            f"{self.service.name} accepted the change but the tag did not "
            f"{'land' if mode == 'add' else 'go'}"
        )

    def add(self, item_id: int, label: str) -> None:
        self._apply(item_id, label, "add")

    def remove(self, item_id: int, label: str) -> None:
        self._apply(item_id, label, "remove")
