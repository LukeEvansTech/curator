"""The JSON API under `/api/v1`, for the phone app.

Each endpoint is drawn from the view its page is drawn from (`week_view`, `review.queue`,
`lists.deleted`) and worded by the pages' own formatters, handed in as `shown`, so the two cannot
disagree. A press goes through `review.press`, the same call the one-at-a-time page makes. No
script runs on the pages, so nothing on the site reads this API.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import buttons, lists, review
from .arr import ArrError, TagClient
from .config import Settings
from .db import DB
from .document import LIBRARIES

DELETED_PAGE = 100  # rows a page of the deleted list holds
ACTIONS = ("keep", "delete", "undo", "unmark", *review.MARKS)
SECTIONS = ("going", "needs_you", "later")  # the lists This week shows on a phone


def attach(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-statements
    app: FastAPI,
    s: Settings,
    db: DB,
    clients: dict[str, TagClient],
    week_view: Callable[..., dict],
    shown: dict[str, Any],
    live_cache: dict,
) -> None:
    """Add the API's routes to the app that serves the pages."""
    gb, when, day = shown["gb"], shown["when"], shown["day"]
    names, service, card_state = shown["names"], shown["service"], shown["card_state"]

    def offers(view: dict, entry: dict, section: str, stand: str) -> list[dict[str, str]]:
        """The buttons for one title: This week's own, with the two marks while it waits."""
        base = card_state(view, entry, section)["buttons"]
        if any(action == "undo" for action, _ in base):
            out = [(action, label, "quiet") for action, label in base]
        elif stand in ("wait", *review.MARKS):
            main, second = review.choices(section, base)
            out = [b for b in main + second if b[0] != stand]
            if stand in review.MARKS:
                out.append(("unmark", "Undo", "quiet"))
        else:
            out = [(action, label, action) for action, label in base]
        return [{"action": a, "label": label, "look": look} for a, label, look in out]

    def item(view: dict, entry: dict, section: str) -> dict[str, Any]:
        library, item_id = view["library"], entry["id"]
        stand = review.standing(view, entry, section)
        state = card_state(view, entry, section)
        due = entry.get("due_at") or ""
        return {
            "library": library,
            "id": item_id,
            "section": section,
            "title": entry["title"],
            "year": int(entry["year"]) if entry.get("year") else None,
            "size_gb": entry.get("size_gb"),
            "size": gb(entry.get("size_gb")),
            "facts": [text for _, text in shown["fact_list"](entry)],
            "imdb": shown["imdb_fact"](entry),
            "imdb_url": shown["imdb_link"](
                view["imdb"].get(item_id, ""), entry["title"], entry.get("year")
            ),
            "by": shown["byline"].get(entry.get("by") or "", ""),
            "reason": entry.get("reason") or "",
            "overview": entry.get("overview") or "",
            "due_at": due,
            "due_day": day(due) if due else "",
            "stand": stand,
            "note": state.get("note", ""),
            "tone": state.get("tone", ""),
            # Only a Keep holds a title back: a pass or a put-off leaves it to the run.
            "goes": section == "going" and stand != "keep",
            "buttons": offers(view, entry, section, stand),
            "poster": f"/poster/{library}/{item_id}",
            "page": f"/title/{library}/{item_id}",
        }

    def library_of(view: dict) -> dict[str, Any]:
        library, doc = view["library"], view["doc"]
        out: dict[str, Any] = {
            "library": library,
            "name": names[library],
            "service": service[library],
            "published": bool(doc),
            "generated_at": "",
            "generated": "",
            "rehearsal": False,
            "next_run_at": "",
            "next_run_day": "",
            "cap": 0,
            "going_count": 0,
            "going_size": gb(0),
            "kept_by_you": 0,
            "counts": {"later": 0, "kept": 0, "needs_you": 0, "deleted": 0},
            "notices": lists.notices(view, when),
            "token": view.get("token", ""),
            **{section: [] for section in SECTIONS},
        }
        if not doc:
            return out
        out.update(
            generated_at=doc["generated_at"],
            generated=when(doc["generated_at"]),
            rehearsal=doc["mode"] != "act",
            next_run_at=doc.get("next_run_at") or "",
            next_run_day=day(doc.get("next_run_at")),
            cap=doc.get("cap") or 0,
            going_count=len(view["going"]),
            going_size=shown["total_gb"](view["going"]),
            kept_by_you=len(doc["going"]) - len(view["going"]),
            counts={k: len(doc[k]) for k in ("later", "kept", "needs_you", "deleted")},
        )
        for section in SECTIONS:
            out[section] = [item(view, entry, section) for entry in doc[section]]
        return out

    def views() -> dict[str, dict]:
        return {library: week_view(library) for library in LIBRARIES}

    def refused(status: int, message: str) -> JSONResponse:
        return JSONResponse({"ok": False, "error": message}, status_code=status)

    @app.get("/api/v1/week")
    def api_week() -> dict:
        """This week, per library: the headline, the notices and the lists."""
        seen = views()
        return {
            "version": s.version,
            "review_left": review.left(seen, "due"),
            "libraries": [library_of(view) for view in seen.values()],
        }

    @app.get("/api/v1/review")
    def api_review(scope: str = "") -> dict:
        """One list to go through a title at a time, in the order the page asks."""
        scope = scope if scope in review.SCOPES else "due"
        seen = views()
        entries = review.queue(seen, scope)
        return {
            "scope": scope,
            "name": review.SCOPE_NAMES[scope],
            "scopes": [
                {"scope": name, "name": review.SCOPE_NAMES[name], "left": review.left(seen, name)}
                for name in review.SCOPES
            ],
            **review.tally(entries),
            "tokens": {library: view.get("token", "") for library, view in seen.items()},
            "unread": [lib for lib, view in seen.items() if view["doc"] and view["live"] is None],
            # A dry run deletes nothing, and a list may be old or unread: said here as on the page.
            "rehearsal": [lib for lib, v in seen.items() if v["doc"] and v["doc"]["mode"] != "act"],
            "notices": {lib: lists.notices(view, when) for lib, view in seen.items()},
            "entries": [item(seen[e["library"]], e["item"], e["section"]) for e in entries],
        }

    @app.post("/api/v1/act")
    async def api_act(request: Request) -> Any:
        """One press on a title of the latest list. Answers with its library as it now stands."""
        try:  # read by hand, so a body that is not a press is refused in the API's own words
            press = json.loads(await request.body())
        except ValueError:
            press = None
        if not isinstance(press, dict):
            return refused(400, "That request is not a press.")
        return await run_in_threadpool(act, press)

    def act(press: dict) -> Any:
        library, item_id = press.get("library"), press.get("id")
        action, token = press.get("action"), press.get("token")
        if library not in LIBRARIES:
            return refused(400, "That request names a library this app does not have.")
        if isinstance(item_id, bool) or not isinstance(item_id, int) or not 0 < item_id < 10**15:
            return refused(400, "That request names no title.")
        if action not in ACTIONS or not isinstance(token, str):
            return refused(400, "That button does not exist.")
        try:
            review.press(
                db, clients[library], s.key(library), library, item_id, action, token[:200]
            )
        except buttons.Refused as exc:
            return refused(exc.status, exc.message)
        except ArrError as exc:
            return refused(502, f"Not done: {exc}")
        finally:
            live_cache.pop(library, None)
        seen = views()
        return {
            "ok": True,
            "review_left": review.left(seen, "due"),
            "library": library_of(seen[library]),
        }

    @app.get("/api/v1/deleted")
    def api_deleted(library: str = "", page: int = 1) -> dict:
        """What the runs took, largest first, a page at a time."""
        lib = library if library in LIBRARIES else ""
        found = lists.deleted(db, lib, page, DELETED_PAGE, s.recycle_days)
        links = {name: shown["imdb_ids"](name) for name in LIBRARIES}
        rows = []
        for row in found["rows"]:
            name, item_id = row["library"], row["item_id"]
            rows.append(
                {
                    "library": name,
                    "id": item_id,
                    "title": row["title"],
                    "year": row["year"],
                    "size_gb": row["size_gb"],
                    "size": gb(row["size_gb"]),
                    "at": row["at"],
                    "when": when(row["at"]),
                    "reason": row["reason"],
                    "dry_run": row["kind"] != "deleted",
                    "in_bin": row["in_bin"],
                    "bin_until_day": day(row["bin_until"]) if row["bin_until"] else "",
                    "imdb_url": shown["imdb_link"](
                        links[name].get(item_id, ""), row["title"], row["year"]
                    ),
                    "poster": f"/poster/{name}/{item_id}",
                    "page": f"/title/{name}/{item_id}",
                }
            )
        return {
            "library": lib,
            "page": found["page_no"],
            "pages": found["pages"],
            "count": found["count"],
            "truncated": found["page_no"] < found["pages"],
            "deleted_count": found["deleted_n"],
            "total_size": gb(found["total"]),
            "recycle_days": found["recycle_days"],
            "rows": rows,
        }
