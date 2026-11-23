"""Going through a list one title at a time.

The page shows the first title still waiting for an answer; a press answers it and the next is
drawn. Keep and Delete instead are This week's own buttons (`buttons.week`), so the tag is the
answer here as it is there. The two answers that change no tag are kept in `reviews`: `pass` (seen,
and left as the list has it) and `later` (put off to the end). Neither reaches the engines: a
title left to go goes at the run whether or not anybody looked at it.
"""

from __future__ import annotations

from typing import Callable

from . import buttons
from .arr import TAG_DELETE, TAG_KEEP
from .db import DB
from .ingest import LOCK

# The lists a person can go through, and the sections of a document each one holds.
SCOPES = {"due": ("going", "needs_you"), "later": ("later",), "kept": ("kept",)}
SCOPE_NAMES = {"due": "Due at the next run", "later": "Later", "kept": "Being kept for a year"}
# What the list means to do with a title in each section. A pass agrees with one proposal, so
# it stands only while the list still carries that proposal: a kept title the judge turns on is
# asked again, and so is one proposed for deletion a second time.
VERDICT = {"going": "delete", "later": "delete", "kept": "keep", "needs_you": "none"}
MARKS = ("pass", "later")
PASS_LABEL = {
    "going": "Let it go",
    "later": "Let it go",
    "kept": "Fine for a year",
    "needs_you": "Leave it",
}
PASS_DONE = {
    "going": "Left to go",
    "later": "Left to go",
    "kept": "Left for the year",
    "needs_you": "Left alone",
}
LATER = ("later", "Decide later")
ANSWER = {TAG_KEEP: "keep", TAG_DELETE: "delete"}  # a decision tag, as an answer here


def choices(section: str, offered: list[tuple[str, str]]) -> tuple[list, list]:
    """The buttons for one title as (main row, second row), each (action, label, look).

    Keep sits on the left and whatever ends in deletion on the right, title after title, so a
    thumb never has to look for them. The pass joins the main row when the list offers only one
    of Keep and Delete instead.
    """
    by_action = dict(offered)
    keep = ("keep", by_action["keep"], "keep") if "keep" in by_action else None
    delete = ("delete", by_action["delete"], "delete") if "delete" in by_action else None
    # Letting a title go ends in its deletion, so it wears that colour; other passes are plain.
    leave = ("pass", PASS_LABEL[section], "delete" if VERDICT[section] == "delete" else "plain")
    later = (*LATER, "quiet")
    if keep and delete:
        return [keep, delete], [leave, later]
    if keep:
        return [keep, leave], [later]
    return [leave, delete] if delete else [leave], [later]


def marks(db: DB, library: str) -> dict[int, dict]:
    """Every pass and put-off on record for a library, by title."""
    rows = db.all("SELECT * FROM reviews WHERE library = ?", library)
    return {row["item_id"]: dict(row) for row in rows}


def stands(mark: dict | None, section: str, item: dict, document_id: int) -> str:
    """The mark that still counts for a title now listed under `section`: "pass", "later" or "".

    A put-off lasts for the list it was made on; the next list asks again. A pass lasts while
    the list carries the proposal it answered: the same thing meant for the title, proposed at
    the same moment (a proposal keeps its date from Later to due).
    """
    if not mark:
        return ""
    if mark["mark"] == "later":
        return "later" if mark["document_id"] == document_id else ""
    same = VERDICT.get(mark["section"]) == VERDICT[section]
    return "pass" if same and mark["proposed_at"] == item["proposed_at"] else ""


def standing(view: dict, item: dict, section: str) -> str:
    """Where a title stands: "keep" or "delete" when that decision tag is on it, "sent" when
    the service took a tag from this app and has not shown it, "pass" or "later" when it was
    answered without a tag, "wait" when it still needs an answer, "" when the list offers nothing
    to press for it. `view` is This week's view of its library."""
    item_id = item["id"]
    if item_id in view["pending"]:
        # Not an answer yet: a tag that never lands leaves the title as it was, to be asked again.
        return "sent"
    live = view.get("live")
    if live is None and item_id in view["mine"]:
        return ANSWER[view["mine"][item_id]]
    # cleanup-keep outranks cleanup-eligible in the engines' plan
    for tag in (TAG_KEEP, TAG_DELETE):
        if item_id in (live or {}).get(tag, frozenset()):
            return ANSWER[tag]
    if not item["actions"]:
        return ""
    return stands(view["marks"].get(item_id), section, item, view["doc_id"]) or "wait"


def still_going(view: dict) -> list[dict]:
    """The going list less what a person has kept since the document was published.

    The document is a week's snapshot; a title kept from it is not deleted, so the
    headline count and size leave it out.
    """
    return [i for i in view["doc"].get("going") or [] if standing(view, i, "going") != "keep"]


def queue(views: dict[str, dict], scope: str) -> list[dict]:
    """Every title a list holds, films first, in the order This week shows them."""
    out = []
    for library, view in views.items():
        for section in SCOPES[scope] if view["doc"] else ():
            for item in view["doc"][section]:
                stand = standing(view, item, section)
                if stand:
                    out.append(
                        {"library": library, "section": section, "item": item, "stand": stand}
                    )
    return out


def left(views: dict[str, dict], scope: str) -> int:
    """How many titles in a list are not yet answered: waiting, put off, or sent and not shown."""
    return sum(entry["stand"] in ("wait", "later", "sent") for entry in queue(views, scope))


def last_answer(
    views: dict[str, dict], entries: list[dict], last: str, undoable: Callable[[dict], bool]
) -> dict | None:
    """The answer just given, said back with the button that takes it away again. `last` is
    `library-id` from the address, so nothing in it is trusted; `undoable` says whether the tag
    on an entry is this app's to take off."""
    library, _, raw = last.partition("-")
    if library not in views or not (raw.isascii() and raw.isdigit() and len(raw) <= 15):
        return None
    entry = next(
        (e for e in entries if e["library"] == library and e["item"]["id"] == int(raw)), None
    )
    if entry is None or entry["stand"] in ("", "wait"):
        return None
    out = {**entry, "token": views[library]["token"], "undo": "unmark", "tone": ""}
    if entry["stand"] == "pass":
        out.update(text=PASS_DONE[entry["section"]])
    elif entry["stand"] == "later":
        out.update(text="Put off")
    elif entry["stand"] == "sent":
        out.update(text="Sent, not shown yet", undo="")
    else:
        kept = entry["stand"] == "keep"
        out.update(
            text="Kept" if kept else "Marked for deletion",
            tone="kept" if kept else "gone",
            undo="undo" if undoable(entry) else "",
        )
    return out


def mark(db: DB, key: str, library: str, item_id: int, kind: str, token: str) -> None:
    """Record a pass or a put-off for a title on the latest list."""
    if kind not in MARKS:
        raise buttons.Refused(400, "That button does not exist.")
    with LOCK:  # the document a press was drawn from is still the latest when the mark lands
        doc_id, section, item = buttons.listed(db, key, library, item_id, token)
        db.run(
            "INSERT INTO reviews (library, item_id, mark, section, proposed_at, document_id)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (library, item_id) DO UPDATE SET"
            " mark = excluded.mark, section = excluded.section,"
            " proposed_at = excluded.proposed_at, document_id = excluded.document_id,"
            " at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
            library,
            item_id,
            kind,
            section,
            item["proposed_at"],
            doc_id,
        )


def unmark(db: DB, key: str, library: str, item_id: int, token: str) -> None:
    """Take a pass or a put-off back, so the title waits for an answer again."""
    with LOCK:
        buttons.listed(db, key, library, item_id, token)
        buttons.clear_mark(db, library, item_id)


def again(db: DB, scope: str) -> None:
    """Bring back every title put off in a list, to be asked once more."""
    for section in SCOPES[scope]:
        db.run("DELETE FROM reviews WHERE mark = 'later' AND section = ?", section)
