"""The allow list: read from Radarr and Sonarr, cached in `catalogue`, never stored as truth here."""

from __future__ import annotations

import datetime as dt
import json
import logging

from .arr import TAG_DELETE, TAGS, ArrError, TagClient
from .db import DB
from .ingest import LOCK

log = logging.getLogger(__name__)

# The engines' rating bar (planner.py KEEP_BAR_*): IMDb 6.5 or higher on 5,000 or more votes.
BAR_SCORE = 6.5
BAR_VOTES = 5000
WHY = {
    "keep": "keep tag",
    "cleanup-keep": "cleanup-keep tag",
    "keep-review": "keep-review tag (legacy)",
    "bar": "rating bar",
}


def meets_bar(library: str, row: dict) -> bool:
    """Whether the rating bar protects a title, as the engines decide it.

    `cleanup-eligible` switches the bar off; on TV the bar never protects a Reality series.
    """
    tags = row["tags"]
    if TAG_DELETE in tags:
        return False
    if library == "tv" and any(g.strip().lower() == "reality" for g in row["genres"]):
        return False
    score, votes = row.get("imdb_score"), row.get("imdb_votes")
    return score is not None and votes is not None and votes >= BAR_VOTES and score >= BAR_SCORE


def reasons(library: str, row: dict) -> list[str]:
    """Why a title is on the allow list; empty when it is not."""
    out = [t for t in ("keep", "cleanup-keep", "keep-review") if t in row["tags"]]
    if meets_bar(library, row):
        out.append("bar")
    return out


def decode(row) -> dict:
    r = dict(row)
    r["tags"] = json.loads(r["tags"])
    r["genres"] = json.loads(r["genres"])
    return r


def refresh(db: DB, library: str, client: TagClient) -> str:
    """Replace one library's cached copy from its service. Returns a line for /ops."""
    # Under the lock the buttons hold, so a tag change cannot land between the read and the write
    # and then be wiped by an older snapshot.
    with LOCK:
        return _refresh(db, library, client)


def reconcile(db: DB, library: str, live: dict[str, frozenset[int]]) -> int:
    """Close every open, confirmed tap whose tag a read has just shown to be gone, so Undo can
    never remove a tag somebody adds back later. Call only under LOCK, with a read taken in it."""
    closed = 0
    for tap in db.all(
        "SELECT id, item_id, tag FROM taps WHERE library = ? AND added = 1 AND confirmed = 1"
        " AND undone_at IS NULL",
        library,
    ):
        if tap["item_id"] not in live.get(tap["tag"], frozenset()):
            db.run(
                "UPDATE taps SET undone_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),"
                " undo_note = 'tag removed outside this app' WHERE id = ? AND undone_at IS NULL",
                tap["id"],
            )
            closed += 1
    return closed


def _refresh(db: DB, library: str, client: TagClient) -> str:
    at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        titles = client.library()
    except ArrError as exc:
        result = {"at": at, "ok": False, "message": str(exc)}
        db.put(f"allow_refresh:{library}", result)
        log.warning("allow list refresh for %s failed: %s", library, exc)
        return str(exc)
    with db.tx() as c:
        c.execute("DELETE FROM catalogue WHERE library = ?", (library,))
        c.executemany(
            "INSERT INTO catalogue (library, id, title, year, size_gb, imdb_score, imdb_votes,"
            " genres, tags, slug) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    library,
                    t["id"],
                    t["title"],
                    t["year"],
                    t["size_gb"],
                    t["imdb_score"],
                    t["imdb_votes"],
                    json.dumps(t["genres"]),
                    json.dumps(t["tags"]),
                    t["slug"],
                )
                for t in titles
            ],
        )
    reconcile(
        db, library, {tag: frozenset(x["id"] for x in titles if tag in x["tags"]) for tag in TAGS}
    )
    message = f"{len(titles):,} titles read"
    db.put(f"allow_refresh:{library}", {"at": at, "ok": True, "message": message})
    return message


def set_tags(db: DB, library: str, item_id: int, tag: str, present: bool) -> None:
    """Keep the cached copy in step after this app changed a tag."""
    row = db.one("SELECT tags FROM catalogue WHERE library = ? AND id = ?", library, item_id)
    if not row:
        return
    tags = set(json.loads(row["tags"]))
    if present:
        tags.add(tag)
    else:
        tags.discard(tag)
    db.run(
        "UPDATE catalogue SET tags = ? WHERE library = ? AND id = ?",
        json.dumps(sorted(tags)),
        library,
        item_id,
    )
