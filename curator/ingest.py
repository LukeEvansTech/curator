"""Keeping an accepted document: the document itself, then the tables the pages read."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from typing import Any

from . import document as d
from .db import DB

# The engines' own run id rule (acting.py RUN_ID); anything else is named by the document instead.
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
LOCK = threading.Lock()  # one publish at a time: the stale check reads before it writes


class Stale(ValueError):
    """Older than the document already held; a replayed old one must not replace a newer one."""


def run_key(doc: dict, document_id: int) -> str:
    """How a run is named in URLs: the engine's run id, or the document's id when it has no usable one."""
    return doc["run_id"] if RUN_ID.match(doc["run_id"]) else f"doc-{document_id}"


def _gb(items: list[dict]) -> float:
    return round(sum(float(i.get("size_gb") or 0) for i in items), 2)


def accept(db: DB, library: str, body: bytes, doc: dict) -> tuple[int, bool]:
    """Store a validated document. Returns (document id, whether it was new)."""
    sha = hashlib.sha256(body).hexdigest()
    with LOCK, db.tx() as c:
        same = c.execute(
            "SELECT id FROM documents WHERE library = ? AND sha256 = ?", (library, sha)
        ).fetchone()
        if same:
            return int(same["id"]), False  # an engine retrying a publish that already landed
        held = c.execute(
            "SELECT generated_at FROM documents WHERE library = ? "
            "ORDER BY generated_at DESC LIMIT 1",
            (library,),
        ).fetchone()
        if held and d.moment(doc["generated_at"]) < d.moment(held["generated_at"]):  # type: ignore[operator]
            raise Stale("an older document than the one already held")
        cur = c.execute(
            "INSERT INTO documents (library, version, run_id, generated_at, sha256, size, body, doc)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                library,
                doc["version"],
                doc["run_id"],
                doc["generated_at"],
                sha,
                len(body),
                body.decode("utf-8"),
                json.dumps(doc, separators=(",", ":")),
            ),
        )
        doc_id = int(cur.lastrowid or 0)
        _record(c, library, doc_id, doc)
        return doc_id, True


def _record(c: sqlite3.Connection, library: str, doc_id: int, doc: dict) -> None:
    key = run_key(doc, doc_id)
    at = doc["generated_at"]
    received = c.execute("SELECT received_at FROM documents WHERE id = ?", (doc_id,)).fetchone()[0]
    summary = doc.get("summary") or {}
    j = d.judge(summary)
    row: dict[str, Any] = {
        "library": library,
        "run_key": key,
        "run_id": doc["run_id"],
        "document_id": doc_id,
        "version": doc["version"],
        "mode": doc["mode"],
        "generated_at": at,
        "received_at": received,
        "next_run_at": doc["next_run_at"],
        "cap": doc["cap"],
        "blocks": json.dumps(doc["blocks"]),
        "going_n": len(doc["going"]),
        "going_gb": _gb(doc["going"]),
        "later_n": len(doc["later"]),
        "kept_n": len(doc["kept"]),
        "needs_n": len(doc["needs_you"]),
        "deleted_n": len(doc["deleted"]),
        "deleted_gb": _gb(doc["deleted"]),
        "summary": json.dumps(summary),
        "digest": summary.get("digest", ""),
        "judge_status": j["status"],
        "judge_batches": j["batches"],
        "judge_failed": j["failed"],
        "tokens_in": j["tokens_in"],
        "tokens_out": j["tokens_out"],
        "model": j["model"],
    }
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    updates = ", ".join(f"{k} = excluded.{k}" for k in row if k not in ("library", "run_key"))
    c.execute(
        f"INSERT INTO runs ({cols}) VALUES ({marks}) ON CONFLICT(library, run_key) DO UPDATE SET "  # nosec B608
        f"{updates}, documents = runs.documents + 1",
        tuple(row.values()),
    )

    for section in d.SECTIONS:
        for item in doc[section]:
            _title(c, library, doc_id, at, item)
            if section == "deleted":
                c.execute(
                    "INSERT OR IGNORE INTO actions (library, item_id, kind, title, year, size_gb,"
                    " reason, at, run_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        library,
                        item["id"],
                        "would-delete" if item["simulated"] or doc["mode"] != "act" else "deleted",
                        item["title"],
                        item["year"],
                        item["size_gb"],
                        item["reason"],
                        at,
                        key,
                    ),
                )
            else:
                _verdict(c, library, key, at, section, item)


def _title(c: sqlite3.Connection, library: str, doc_id: int, at: str, item: dict) -> None:
    c.execute(
        "INSERT INTO titles (library, id, title, year, size_gb, imdb_score, imdb_votes, plays,"
        " finished_by, origin, requested_by, overview, first_seen, last_seen, document_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(library, id) DO UPDATE SET title = excluded.title, year = excluded.year,"
        " size_gb = excluded.size_gb, imdb_score = excluded.imdb_score,"
        " imdb_votes = excluded.imdb_votes, plays = excluded.plays,"
        " finished_by = excluded.finished_by, origin = excluded.origin,"
        " requested_by = excluded.requested_by, overview = excluded.overview,"
        " last_seen = excluded.last_seen, document_id = excluded.document_id",
        (
            library,
            item["id"],
            item["title"],
            item["year"],
            item["size_gb"],
            item["imdb_score"],
            item["imdb_votes"],
            item["plays"],
            item["finished_by"],
            item["origin"],
            item["requested_by"],
            item["overview"],
            at,
            at,
            doc_id,
        ),
    )


VERDICT_FIELDS = ("section", "verdict", "by", "reason", "proposed_at", "due_at", "simulated")


def _verdict(
    c: sqlite3.Connection, library: str, key: str, at: str, section: str, item: dict
) -> None:
    """A new row when the verdict differs from the title's last one; otherwise extend that row."""
    now = {**{k: item[k] for k in VERDICT_FIELDS if k != "section"}, "section": section}
    now["simulated"] = int(now["simulated"])
    last = c.execute(
        "SELECT * FROM verdicts WHERE library = ? AND item_id = ? ORDER BY id DESC LIMIT 1",
        (library, item["id"]),
    ).fetchone()
    if last and all(last[k] == now[k] for k in VERDICT_FIELDS):
        c.execute(
            "UPDATE verdicts SET last_run = ?, last_seen = ? WHERE id = ?", (key, at, last["id"])
        )
        return
    c.execute(
        "INSERT INTO verdicts (library, item_id, section, verdict, by, reason, proposed_at, due_at,"
        " simulated, first_run, last_run, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (library, item["id"], *(now[k] for k in VERDICT_FIELDS), key, key, at, at),
    )
