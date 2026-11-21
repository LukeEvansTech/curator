"""The buttons: the only writes this app makes outside its own database.

This week (/): Keep adds cleanup-keep, Delete instead adds cleanup-eligible, Undo removes the tag
that button added. Only for ids in the latest document, only the actions it offers for that id, and
undo only for a tag the taps table says this app added. Allow list (/allow): add or remove
cleanup-keep on a title the services listed. Nothing else, and never the `keep` tag.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from dataclasses import dataclass

from . import allow
from .arr import TAG_DELETE, TAG_KEEP, TagClient, Unconfirmed
from .db import DB
from .document import find, moment
from .ingest import LOCK

TAG_FOR = {"keep": TAG_KEEP, "delete": TAG_DELETE}
# An add the service took but had not shown is this app's only if the tag appears within this
# long of the press; after that the tag could as well be somebody else's, so it is never undone.
PENDING_GRACE = dt.timedelta(minutes=2)


class Refused(Exception):
    """A press this app will not carry out, with the status to answer it with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Done:
    message: str
    changed: bool


def form_token(key: str, library: str, document_id: int) -> str:
    """What a This week button must send back: proof it was drawn from the current document."""
    if not key:
        return ""
    msg = f"form\n{library}\n{document_id}".encode()
    return hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()


def open_tap(db: DB, library: str, item_id: int, page: str = "week"):
    """The latest press on a page that added a tag and has not been undone."""
    return db.one(
        "SELECT * FROM taps WHERE library = ? AND item_id = ? AND page = ? AND added = 1"
        " AND undone_at IS NULL ORDER BY id DESC LIMIT 1",
        library,
        item_id,
        page,
    )


def settle(db: DB, library: str, item_id: int, live: dict[str, frozenset[int]], page: str = "week"):
    """The open tap on a title, with a pending add confirmed or given up as the tags now show.

    Returns None when there is none; a tap whose `confirmed` is still 0 is waiting.
    """
    tap = open_tap(db, library, item_id, page)
    if tap is None or tap["confirmed"]:
        return tap
    at = moment(tap["at"])
    fresh = at is not None and dt.datetime.now(dt.timezone.utc) - at <= PENDING_GRACE
    if fresh and item_id in live.get(tap["tag"], frozenset()):
        db.run("UPDATE taps SET confirmed = 1 WHERE id = ?", tap["id"])
        return open_tap(db, library, item_id, page)
    if not fresh:
        close_tap(db, tap["id"], "never confirmed by the service")
        return None
    return tap


def close_tap(db: DB, tap_id: int, note: str) -> None:
    db.run(
        "UPDATE taps SET undone_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), undo_note = ?"
        " WHERE id = ? AND undone_at IS NULL",
        note,
        tap_id,
    )


def _record(
    db: DB,
    library: str,
    item_id: int,
    page: str,
    action: str,
    tag: str,
    added: bool,
    doc_id,
    confirmed: bool = True,
):
    db.run(
        "INSERT INTO taps (library, item_id, page, action, tag, added, confirmed, document_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        library,
        item_id,
        page,
        action,
        tag,
        int(added),
        int(confirmed),
        doc_id,
    )


def week(
    db: DB, client: TagClient, key: str, library: str, item_id: int, action: str, token: str
) -> Done:
    """One This week button."""
    if action not in ("keep", "delete", "undo"):
        raise Refused(400, "That button does not exist.")
    if not client.configured:
        raise Refused(503, "The service for this library is not configured.")
    with LOCK:  # held from the document check to the tag change, so a publish cannot slip between
        latest = db.latest(library)
        if latest is None:
            raise Refused(404, "No list has been published for that library yet.")
        doc_id, doc = latest
        expected = form_token(key, library, doc_id)
        if not expected or not hmac.compare_digest(expected.encode(), token.encode()):
            raise Refused(409, "The list has changed since this page was loaded. Reload it.")
        found = find(doc, item_id)
        if found is None:
            raise Refused(404, "That title is not on the current list.")
        item = found[1]
        live = client.tagged()
        mine = settle(db, library, item_id, live)
        if mine and not mine["confirmed"]:
            raise Refused(
                409, "Still waiting for the service to show the last change. Try again in a minute."
            )
        if mine and item_id not in live.get(mine["tag"], frozenset()):
            # The tag this app added has been taken off elsewhere: nothing is left to undo.
            close_tap(db, mine["id"], "tag removed outside this app")
            mine = None

        if action == "undo":
            if not mine:
                raise Refused(409, "There is nothing of this app's to undo on that title.")
            client.remove(item_id, mine["tag"])
            close_tap(db, mine["id"], "undone")
            allow.set_tags(db, library, item_id, mine["tag"], False)
            return Done("Undone.", True)

        tag = TAG_FOR[action]
        if action not in item["actions"]:
            raise Refused(403, "That is not something this list offers for that title.")
        if mine:
            raise Refused(409, "Undo what this app already did to that title first.")
        if item_id in live.get(tag, frozenset()):
            # Already tagged elsewhere: nothing to add, and nothing this app may later undo.
            return Done(f"It already carries {tag}.", False)
        try:
            client.add(item_id, tag)
        except Unconfirmed:
            # Taken but not shown yet: pending, and this app's only if it lands soon (settle).
            _record(db, library, item_id, "week", action, tag, True, doc_id, confirmed=False)
            raise
        # Recorded once the tag is confirmed, so undo never removes a tag this app did not add.
        _record(db, library, item_id, "week", action, tag, True, doc_id)
        allow.set_tags(db, library, item_id, tag, True)
        return Done("Kept." if action == "keep" else "Marked for deletion.", True)


def allow_list(db: DB, client: TagClient, library: str, item_id: int, action: str) -> Done:
    """Add or remove cleanup-keep from the allow list page."""
    if action not in ("add", "remove"):
        raise Refused(400, "That button does not exist.")
    if not db.one("SELECT 1 FROM catalogue WHERE library = ? AND id = ?", library, item_id):
        raise Refused(404, "That title is not in the library as last read. Refresh the list.")
    if not client.configured:
        raise Refused(503, "The service for this library is not configured.")
    with LOCK:
        has = item_id in client.tagged().get(TAG_KEEP, frozenset())
        if action == "add":
            if has:
                allow.set_tags(db, library, item_id, TAG_KEEP, True)
                return Done(f"It already carries {TAG_KEEP}.", False)
            client.add(item_id, TAG_KEEP)
            _record(db, library, item_id, "allow", "allow-add", TAG_KEEP, True, None)
            allow.set_tags(db, library, item_id, TAG_KEEP, True)
            return Done(f"Added {TAG_KEEP}.", True)
        if not has:
            allow.set_tags(db, library, item_id, TAG_KEEP, False)
            return Done(f"It does not carry {TAG_KEEP}.", False)
        client.remove(item_id, TAG_KEEP)
        _record(db, library, item_id, "allow", "allow-remove", TAG_KEEP, False, None)
        # A Keep pressed on This week is gone now too; its undo has nothing left to remove.
        for page in ("week", "allow"):
            tap = open_tap(db, library, item_id, page)
            if tap and tap["tag"] == TAG_KEEP:
                close_tap(db, tap["id"], "removed on the allow list")
        allow.set_tags(db, library, item_id, TAG_KEEP, False)
        return Done(f"Removed {TAG_KEEP}.", True)
