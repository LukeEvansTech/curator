"""What a page and the JSON API both show, read once here so the two never disagree."""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from .db import DB
from .document import moment


def notices(view: dict, when: Callable[[Any], str]) -> list[dict[str, Any]]:
    """The warnings above a library's list on This week, in the order they are shown. Each is
    `tone` ("bad" for a fault, else empty), a `lead`, the `text` after it and any `points`."""
    doc = view.get("doc")
    if not doc:
        return []
    out: list[dict[str, Any]] = []

    def add(tone: str, lead: str, text: str = "", points: list[str] | None = None) -> None:
        out.append({"tone": tone, "lead": lead, "text": text, "points": points or []})

    if doc["mode"] != "act":
        add("", "Dry run.", "Nothing is deleted yet; the buttons still set real tags.")
    if view.get("stale"):
        add(
            "bad",
            "This list is old.",
            f"No document has arrived since {when(doc['generated_at'])}. Check the run on Ops.",
        )
    if doc.get("blocks"):
        add("", "The last run was blocked, so it deleted nothing.", points=list(doc["blocks"]))
    if view.get("live") is None:
        add(
            "bad",
            "The live tags could not be read.",
            f"{view.get('error', '')}. The buttons may not show a tag set elsewhere.",
        )
    return out


def deleted(db: DB, library: str, page_no: int, per_page: int, recycle_days: int) -> dict[str, Any]:
    """One page of what the runs took, largest first, with the totals over every page. `library`
    is a known library or empty for both."""
    now = dt.datetime.now(dt.timezone.utc)
    where = "WHERE (? = '' OR a.library = ?)"
    count = db.count(f"SELECT count(*) FROM actions a {where}", library, library)  # nosec B608
    pages = max(1, -(-count // per_page))
    page_no = min(max(page_no, 1), pages)
    rows = []
    for r in db.all(
        "SELECT a.*, c.slug FROM actions a LEFT JOIN catalogue c"  # nosec B608
        f" ON c.library = a.library AND c.id = a.item_id {where}"
        " ORDER BY a.size_gb DESC, a.at DESC, a.id DESC LIMIT ? OFFSET ?",
        library,
        library,
        per_page,
        (page_no - 1) * per_page,
    ):
        row = dict(r)
        at = moment(row["at"])
        until = at + dt.timedelta(days=recycle_days) if at else None
        row["bin_until"] = until.isoformat() if until else ""
        row["in_bin"] = bool(until and until > now and row["kind"] == "deleted")
        rows.append(row)
    sums = db.one(
        "SELECT coalesce(sum(size_gb), 0) AS gb, count(*) AS n FROM actions a"  # nosec B608
        f" {where} AND a.kind = 'deleted'",
        library,
        library,
    )
    return {
        "rows": rows,
        "library": library,
        "total": sums["gb"] if sums else 0,
        "deleted_n": sums["n"] if sums else 0,
        "count": count,
        "page_no": page_no,
        "pages": pages,
        "recycle_days": recycle_days,
    }
