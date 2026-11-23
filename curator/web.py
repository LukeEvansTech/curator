"""Pages and the publish endpoint. The engines' push notification opens `/`."""

from __future__ import annotations

import datetime as dt
import hmac
import json
import logging
import math
import os
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import allow, buttons, ingest, review
from .arr import (
    POSTER_WIDTHS,
    TAG_DELETE,
    TAG_KEEP,
    ArrError,
    TagClient,
    image_type,
    radarr,
    sonarr,
)
from .config import Settings
from .db import DB
from .document import JUDGE_FIELDS, LIBRARIES, Invalid, find, moment, normalise, sign

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
NAMES = {"film": "Films", "tv": "TV"}
SERVICE = {"film": "Radarr", "tv": "Sonarr"}
ORIGINS = {"feed": "added by a list", "requested": "requested", "unknown": "origin unknown"}
BYLINE = {"judge": "Judge", "engine": "Could not be assessed", "person": "You"}
SIGNATURE_HEADER = "x-curator-signature"
SHOWN_JUDGE = {name for names in JUDGE_FIELDS.values() for name in names}
LIVE_TTL = 10.0  # seconds a read of the live tags serves a page before it is read again
ALLOW_PAGE = 300  # rows the allow list shows at once
DELETED_PAGE = 100  # rows a page of /deleted shows
RUNS_PAGE = 50  # runs a page of /runs shows
IMDB = "https://www.imdb.com"
POSTER_SECONDS = 7 * 86400  # how long a browser keeps a poster this app holds
NO_POSTER_SECONDS = 3600  # and how long it takes the placeholder for an answer
NO_POSTER = "/static/poster.svg"
POLICY = (
    "default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; "
    "manifest-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


def gb(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(f):
        return ""
    return f"{f / 1000:,.2f} TB" if f >= 1000 else f"{f:,.1f} GB"


def imdb_fact(item: dict) -> str:
    """The rating as the IMDb link says it; plain "IMDb" when the record has no score."""
    if item.get("imdb_score") is None:
        return "IMDb"
    votes = f" on {int(item['imdb_votes']):,} votes" if item.get("imdb_votes") is not None else ""
    return f"IMDb {float(item['imdb_score']):.1f}{votes}"


def fact_list(item: dict) -> list[tuple[str, str]]:
    """Size, plays and origin, each with its kind; a fact the record lacks is left out."""
    parts = []
    if item.get("size_gb") is not None:
        parts.append(("size", gb(item["size_gb"])))
    if item.get("plays") is not None:
        n = int(item["plays"])
        parts.append(("plays", f"{n} play{'' if n == 1 else 's'}"))
    if item.get("finished_by"):
        parts.append(("plays", f"finished by {int(item['finished_by'])}"))
    origin = ORIGINS.get(item.get("origin") or "", "")
    if item.get("origin") == "requested" and item.get("requested_by"):
        origin = f"requested by {item['requested_by']}"
    if origin:
        parts.append(("origin", origin))
    return parts


def imdb_link(imdb_id: str, title: Any, year: Any) -> str:
    """The title's IMDb page when its id is known, else IMDb's own title search for it."""
    if imdb_id:
        return f"{IMDB}/title/{imdb_id}/"
    query = f"{title} {year}" if year else str(title)
    return f"{IMDB}/find/?" + urllib.parse.urlencode({"q": query, "s": "tt"})


def keep_file(path: Path, data: bytes) -> None:
    """Write beside the target, then rename, so a reader never meets half a poster. A volume
    that cannot be written costs only the next fetch."""
    name = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
            name = tmp.name
            tmp.write(data)
        os.replace(name, path)
    except OSError as exc:
        log.warning("poster %s could not be kept: %s", path.name, exc)
        if name:
            Path(name).unlink(missing_ok=True)


def service_link(s: Settings, library: str, slug: str) -> str:
    """The title's page in Radarr or Sonarr, when a browser-facing base URL is set."""
    base = s.link_base(library)
    if not base or not slug:
        return ""
    kind = "movie" if library == "film" else "series"
    return f"{base}/{kind}/{urllib.parse.quote(slug, safe='')}"


def default_clients(s: Settings) -> dict[str, TagClient]:
    return {
        "film": TagClient(radarr(s.radarr_url, s.radarr_key)),
        "tv": TagClient(sonarr(s.sonarr_url, s.sonarr_key)),
    }


def create_app(
    s: Settings,
    db: DB | None = None,
    clients: dict[str, TagClient] | None = None,
    start_scheduler: bool = False,
) -> FastAPI:
    db = db or DB(s.db_path)
    clients = clients or default_clients(s)
    app = FastAPI(title="curator", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tz = ZoneInfo(s.timezone)
    live_cache: dict[str, tuple[float, dict[str, frozenset[int]]]] = {}
    refreshing = threading.Lock()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Refuse a browser write another site sent (no login here, so no CSRF cookie), and send
        a policy that lets nothing load from anywhere else and no script run at all."""
        if request.method in ("POST", "PUT", "DELETE"):
            site = request.headers.get("sec-fetch-site", "")
            origin = request.headers.get("origin", "")
            if origin == "null" and site == "same-origin":
                # A page under a no-referrer policy has its origin withheld even from its own
                # site. The browser's own account of where the press came from then decides.
                origin = ""
            origin = origin or request.headers.get("referer") or ""
            host = urllib.parse.urlsplit(origin).netloc
            if (site and site not in ("same-origin", "none")) or (
                origin and host != request.headers.get("host", "")
            ):
                return PlainTextResponse("cross-site request refused", status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # This site's own address goes to this site and to nobody else. `no-referrer` would
        # also blank the Origin of this site's own presses, which the check above reads.
        response.headers["Referrer-Policy"] = "same-origin"
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response

    tpl = Jinja2Templates(directory=HERE / "templates")  # autoescape on for .html
    assert tpl.env.autoescape  # nosec B101: every page relies on it

    def local(stamp: Any) -> dt.datetime | None:
        at = moment(stamp) if isinstance(stamp, str) else None
        return at.astimezone(tz) if at else None

    def when(stamp: Any) -> str:
        at = local(stamp)
        return f"{at:%a %-d %b %Y, %H:%M}" if at else "unknown"

    def day(stamp: Any) -> str:
        at = local(stamp)
        return f"{at:%A %-d %B}" if at else "the next run"

    def shortday(stamp: Any) -> str:
        at = local(stamp)
        return f"{at:%a %-d %b}" if at else "the next run"

    def ago(stamp: Any) -> str:
        at = moment(stamp) if isinstance(stamp, str) else None
        if not at:
            return "never"
        secs = (dt.datetime.now(dt.timezone.utc) - at).total_seconds()
        if secs < 3600:
            return f"{max(int(secs // 60), 0)} min ago"
        if secs < 86400 * 2:
            return f"{int(secs // 3600)} h ago"
        return f"{int(secs // 86400)} days ago"

    def cost(tokens_in: Any, tokens_out: Any) -> str:
        if tokens_in is None and tokens_out is None:
            return ""
        usd = (tokens_in or 0) / 1e6 * s.price_in + (tokens_out or 0) / 1e6 * s.price_out
        return f"${usd:,.2f}"

    def total_gb(items: list[dict]) -> str:
        return gb(sum(float(i.get("size_gb") or 0) for i in items))

    tpl.env.filters.update(
        gb=gb,
        when=when,
        day=day,
        shortday=shortday,
        ago=ago,
        fact_list=fact_list,
        imdb_fact=imdb_fact,
        total_gb=total_gb,
    )
    tpl.env.globals.update(
        version=s.version,
        names=NAMES,
        service=SERVICE,
        byline=BYLINE,
        cost=cost,
        imdb_link=imdb_link,
    )

    def page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        return tpl.TemplateResponse(request, name, ctx, status_code=status)

    def problem(request: Request, status: int, message: str, back: str = "") -> HTMLResponse:
        return page(
            request, "problem.html", status=status, status_code=status, message=message, back=back
        )

    def imdb_ids(library: str) -> dict[int, str]:
        """Every IMDb id on record for a library, by the service's id for the title."""
        rows = db.all("SELECT id, imdb_id FROM links WHERE library = ?", library)
        return {r["id"]: r["imdb_id"] for r in rows}

    def live(library: str, fresh: bool = False) -> dict[str, frozenset[int]]:
        cached = live_cache.get(library)
        if cached and not fresh and time.monotonic() - cached[0] < LIVE_TTL:
            return cached[1]
        tags = clients[library].tagged()
        live_cache[library] = (time.monotonic(), tags)
        return tags

    def refresh_all() -> dict[str, str]:
        if not refreshing.acquire(blocking=False):  # pylint: disable=consider-using-with
            return {}
        try:
            return {lib: allow.refresh(db, lib, clients[lib]) for lib in LIBRARIES}
        finally:
            refreshing.release()

    if start_scheduler:
        from . import scheduler

        scheduler.start(s.allow_schedule, s.timezone, refresh_all, name="allow list refresh")
        threading.Thread(target=refresh_all, name="allow-first-read", daemon=True).start()

    def reject(library: str, status: int, reason: str, size: int) -> JSONResponse:
        name = library if library in LIBRARIES else "unknown"
        db.run(
            "INSERT INTO rejects (library, status, reason, size) VALUES (?, ?, ?, ?)",
            name,
            status,
            reason[:300],
            size,
        )
        db.run("DELETE FROM rejects WHERE id <= (SELECT max(id) - 1000 FROM rejects)")
        log.warning("publish for %s refused (%s): %s", name, status, reason)
        return JSONResponse({"ok": False, "error": reason}, status_code=status)

    # --- the engines' side ---
    @app.post("/publish/{library}")
    async def publish(library: str, request: Request):
        """Accept one library's review document, if that library's engine signed it."""
        if library not in LIBRARIES:
            return reject(library, 404, "unknown library", 0)
        key = s.key(library)
        if not key:
            # With no key every signature would be computable by anyone; refuse rather than accept.
            return reject(library, 503, "this library has no key configured", 0)
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > s.max_publish_bytes:
            return reject(library, 413, "document too large", int(declared))
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > s.max_publish_bytes:
                return reject(library, 413, "document too large", len(body))
        raw = bytes(body)
        given = request.headers.get(SIGNATURE_HEADER, "")
        # Checked before the body is parsed, and in constant time.
        if not hmac.compare_digest(sign(key, library, raw).encode(), given.encode()):
            return reject(library, 401, "signature missing or wrong", len(raw))
        try:
            doc = normalise(library, json.loads(raw))
            # in a worker thread: the shared lock can be held across a service call by a press
            doc_id, new = await run_in_threadpool(ingest.accept, db, library, raw, doc)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            return reject(library, 400, "the document is not JSON", len(raw))
        except Invalid as exc:
            return reject(library, 400, str(exc), len(raw))
        except ingest.Stale as exc:
            return reject(library, 409, str(exc), len(raw))
        listed = sum(len(doc[k]) for k in ("going", "later", "kept", "needs_you"))
        return {"ok": True, "library": library, "document": doc_id, "new": new, "titles": listed}

    # --- health ---
    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": s.version}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        lines = [
            "# HELP curator_last_document_timestamp_seconds When the newest accepted review "
            "document for a library arrived.",
            "# TYPE curator_last_document_timestamp_seconds gauge",
        ]
        rows: dict[str, list[str]] = {
            "curator_documents_total": ["# TYPE curator_documents_total counter"],
            "curator_going_titles": ["# TYPE curator_going_titles gauge"],
            "curator_going_gigabytes": ["# TYPE curator_going_gigabytes gauge"],
            "curator_allow_refresh_ok": ["# TYPE curator_allow_refresh_ok gauge"],
            "curator_catalogue_titles": ["# TYPE curator_catalogue_titles gauge"],
        }
        for lib in LIBRARIES:
            last = db.one(
                "SELECT CAST(strftime('%s', received_at) AS INTEGER) AS t, count(*) OVER () AS n"
                " FROM documents WHERE library = ? ORDER BY id DESC LIMIT 1",
                lib,
            )
            run = db.one(
                "SELECT going_n, going_gb FROM runs WHERE library = ? "
                "ORDER BY generated_at DESC LIMIT 1",
                lib,
            )
            cat = db.one("SELECT count(*) AS n FROM catalogue WHERE library = ?", lib)
            ok = db.get(f"allow_refresh:{lib}", {}).get("ok")
            label = f'{{library="{lib}"}}'
            lines.append(
                f"curator_last_document_timestamp_seconds{label} {last['t'] if last else 0}"
            )
            rows["curator_documents_total"].append(
                f"curator_documents_total{label} {last['n'] if last else 0}"
            )
            rows["curator_going_titles"].append(
                f"curator_going_titles{label} {run['going_n'] if run else 0}"
            )
            rows["curator_going_gigabytes"].append(
                f"curator_going_gigabytes{label} {run['going_gb'] if run else 0}"
            )
            rows["curator_allow_refresh_ok"].append(
                f"curator_allow_refresh_ok{label} {int(bool(ok))}"
            )
            rows["curator_catalogue_titles"].append(
                f"curator_catalogue_titles{label} {cat['n'] if cat else 0}"
            )
        rejected = db.one("SELECT count(*) AS n FROM rejects")
        for block in rows.values():
            lines.extend(block)
        lines.append("# TYPE curator_publish_rejected gauge")
        lines.append(f"curator_publish_rejected {rejected['n'] if rejected else 0}")
        return "\n".join(lines) + "\n"

    # --- This week ---
    def week_view(library: str, links: bool = True) -> dict:
        latest = db.latest(library)
        view: dict[str, Any] = {"library": library, "doc": None, "live": None, "error": ""}
        if latest is None:
            return view
        doc_id, doc = latest
        view.update(
            doc=doc, doc_id=doc_id, token=buttons.form_token(s.key(library), library, doc_id)
        )
        at = moment(doc["generated_at"])
        view["stale"] = bool(at and (dt.datetime.now(dt.timezone.utc) - at).days >= s.stale_days)
        view["run_key"] = ingest.run_key(doc, doc_id)
        view["imdb"] = imdb_ids(library) if links else {}
        view["marks"] = review.marks(db, library)
        ids = {i["id"] for k in ("going", "later", "kept", "needs_you") for i in doc.get(k) or []}
        # Taps are reconciled with the tags (a pending add confirmed, a removed tag's tap closed)
        # only under the lock the buttons hold and on a read taken inside it, so an older read
        # can never close a tap a press has just recorded. When a press holds the lock, the page
        # shows the taps as they stand and the next load reconciles.
        reconciled = ingest.LOCK.acquire(blocking=False)  # pylint: disable=consider-using-with
        try:
            try:
                view["live"] = live(library, fresh=reconciled)
            except ArrError as exc:
                view["error"] = str(exc)
            if reconciled and view["live"] is not None:
                allow.reconcile(db, library, view["live"])  # off-list titles' taps too
            # One read finds the few titles with a press still open; only those are settled.
            open_taps = db.all(
                "SELECT DISTINCT item_id FROM taps WHERE library = ? AND page = 'week'"
                " AND added = 1 AND undone_at IS NULL",
                library,
            )
            taps = {}
            for item_id in ids.intersection(row["item_id"] for row in open_taps):
                if reconciled and view["live"] is not None:
                    taps[item_id] = buttons.settle(db, library, item_id, view["live"])
                else:
                    taps[item_id] = buttons.open_tap(db, library, item_id)
        finally:
            if reconciled:
                ingest.LOCK.release()
        mine, pending = {}, {}
        for item_id, tap in taps.items():
            if tap and not tap["confirmed"]:
                pending[item_id] = tap["tag"]
            elif tap:
                mine[item_id] = tap["tag"]
        view["mine"], view["pending"] = mine, pending
        view["going"] = review.still_going(view)
        return view

    def card_state(view: dict, item: dict, section: str) -> dict:
        """What a card says about its tags, and which buttons it offers."""
        if section == "deleted":
            return {"note": "", "buttons": []}
        lv = view.get("live")
        has_keep = lv is not None and item["id"] in lv.get(TAG_KEEP, frozenset())
        has_del = lv is not None and item["id"] in lv.get(TAG_DELETE, frozenset())
        if item["id"] in view["pending"]:
            return {
                "note": f"Sent to {SERVICE[view['library']]}; waiting for it to show. Reload in a minute.",
                "tone": "",
                "buttons": [],
            }
        mine = view["mine"].get(item["id"])
        if mine and lv is not None and item["id"] not in lv.get(mine, frozenset()):
            mine = None
        if mine == TAG_KEEP:
            return {
                "note": "Kept. It will not be deleted.",
                "tone": "kept",
                "buttons": [("undo", "Undo")],
            }
        if mine == TAG_DELETE and has_keep:
            # cleanup-keep outranks cleanup-eligible in the engines' plan
            return {
                "note": f"Kept: it carries {TAG_KEEP}, which outranks this app's Delete instead.",
                "tone": "kept",
                "buttons": [("undo", "Undo Delete instead")],
            }
        if mine == TAG_DELETE:
            return {
                "note": "Marked for deletion at the next run.",
                "tone": "gone",
                "buttons": [("undo", "Undo")],
            }
        if has_keep:
            return {
                "note": f"Kept: it carries {TAG_KEEP}, set outside this app.",
                "tone": "kept",
                "buttons": [],
            }
        out: dict[str, Any] = {"note": "", "buttons": []}
        if has_del:
            out.update(
                note=f"Marked for deletion: it carries {TAG_DELETE}, set outside this app.",
                tone="gone",
            )
        if "keep" in item["actions"]:
            # The judge already keeps these for a year; this Keep is the lasting one.
            out["buttons"].append(("keep", "Keep forever" if section == "kept" else "Keep"))
        if "delete" in item["actions"] and not has_del:
            out["buttons"].append(("delete", "Delete instead"))
        return out

    tpl.env.globals["card_state"] = card_state

    def live_or_none(library: str) -> dict[str, frozenset[int]] | None:
        """The decision tags read now, or None when the service is unset or could not be read."""
        if not clients[library].configured:
            return None
        # fresh: one tag read per library per page, and a reload must show a change at once.
        # Under the lock (when free) the read also closes taps whose tag it shows gone.
        held = ingest.LOCK.acquire(blocking=False)  # pylint: disable=consider-using-with
        try:
            tags = live(library, fresh=True)
            if held:
                allow.reconcile(db, library, tags)
            return tags
        except ArrError:
            return None
        finally:
            if held:
                ingest.LOCK.release()

    def overlay(row: dict, tags: dict[str, frozenset[int]] | None) -> dict:
        """A cached title with the two decision tags as the service holds them now, so a change
        made since the last full read (here or elsewhere) shows on the next page load."""
        if tags is not None:
            held = set(row["tags"]) - {TAG_KEEP, TAG_DELETE}
            row["tags"] = sorted(held | {t for t in (TAG_KEEP, TAG_DELETE) if row["id"] in tags[t]})
        return row

    # --- one at a time ---
    @app.get("/review", response_class=HTMLResponse)
    def review_page(request: Request, scope: str = "", last: str = ""):
        scope = scope if scope in review.SCOPES else "due"
        views = {lib: week_view(lib, links=False) for lib in LIBRARIES}
        entries = review.queue(views, scope)
        waiting = [e for e in entries if e["stand"] == "wait"]
        tally = {k: sum(e["stand"] == k for e in entries) for k in ("keep", "delete", "pass")}
        put_off = sum(e["stand"] == "later" for e in entries)
        sent = sum(e["stand"] == "sent" for e in entries)
        now = waiting[0] if waiting else None
        if now:
            view = views[now["library"]]
            known = db.one(
                "SELECT imdb_id FROM links WHERE library = ? AND id = ?",
                now["library"],
                now["item"]["id"],
            )
            main, second = review.choices(
                now["section"], card_state(view, now["item"], now["section"])["buttons"]
            )
            now = {
                **now,
                "view": view,
                "main": main,
                "second": second,
                "out": imdb_link(
                    known["imdb_id"] if known else "", now["item"]["title"], now["item"]["year"]
                ),
            }
        return page(
            request,
            "review.html",
            nav="week",
            focus=bool(now),
            scope=scope,
            scope_name=review.SCOPE_NAMES[scope],
            any_doc=any(v["doc"] for v in views.values()),
            total=len(entries),
            answered=len(entries) - len(waiting) - put_off - sent,
            put_off=put_off,
            sent=sent,
            tally=tally,
            now=now,
            ahead=waiting[1] if len(waiting) > 1 else None,
            last=review.last_answer(
                views,
                entries,
                last[:40],
                # Only a tag this app added can be taken off again; one set elsewhere stays.
                lambda e: ("undo", "Undo")
                in card_state(views[e["library"]], e["item"], e["section"])["buttons"],
            ),
            others=[
                (name, review.SCOPE_NAMES[name], review.left(views, name))
                for name in review.SCOPES
                if name != scope
            ],
            unread=[lib for lib, v in views.items() if v["doc"] and v["live"] is None],
        )

    @app.post("/review/act")
    def review_act(
        request: Request,
        library: str = Form(""),
        id: str = Form(""),
        action: str = Form(""),
        token: str = Form(""),
        scope: str = Form(""),
    ):
        scope = scope if scope in review.SCOPES else "due"
        back = "/review" if scope == "due" else f"/review?scope={scope}"
        if library not in LIBRARIES:
            return problem(request, 400, "That request names a library this app does not have.")
        if not (id.isascii() and id.isdigit() and len(id) <= 15):
            return problem(request, 400, "That request names no title.")
        item_id, key = int(id), s.key(library)
        try:
            if action in review.MARKS:
                review.mark(db, key, library, item_id, action, token[:200])
            elif action == "unmark":
                review.unmark(db, key, library, item_id, token[:200])
            else:
                buttons.week(db, clients[library], key, library, item_id, action, token[:200])
        except buttons.Refused as exc:
            # A second press of the same button finds the first already done: carry on.
            tap = buttons.open_tap(db, library, item_id)
            wanted = buttons.TAG_FOR.get(action)
            # (a press still waiting on the service is not done, and says so)
            landed = bool(tap and tap["confirmed"] and tap["tag"] == wanted)
            repeat = (tap is None) if action == "undo" else landed
            if exc.status != 409 or not repeat:
                return problem(request, exc.status, exc.message, back=back)
        except ArrError as exc:
            return problem(request, 502, f"Not done: {exc}", back=back)
        finally:
            live_cache.pop(library, None)
        if action not in ("undo", "unmark"):
            back += f"{'&' if '?' in back else '?'}last={library}-{item_id}"
        return RedirectResponse(back, status_code=303)

    @app.post("/review/again")
    def review_again(scope: str = Form("")):
        scope = scope if scope in review.SCOPES else "due"
        review.again(db, scope)
        return RedirectResponse(
            "/review" if scope == "due" else f"/review?scope={scope}", status_code=303
        )

    @app.get("/", response_class=HTMLResponse)
    def week(request: Request):
        views = {lib: week_view(lib) for lib in LIBRARIES}
        return page(
            request,
            "week.html",
            nav="week",
            views=list(views.values()),
            left=review.left(views, "due"),
        )

    @app.post("/act")
    def act(
        request: Request,
        library: str = Form(""),
        id: str = Form(""),
        action: str = Form(""),
        token: str = Form(""),
    ):
        if library not in LIBRARIES:
            return problem(request, 400, "That request names a library this app does not have.")
        if not (id.isascii() and id.isdigit() and len(id) <= 15):
            return problem(request, 400, "That request names no title.")
        try:
            buttons.week(
                db, clients[library], s.key(library), library, int(id), action, token[:200]
            )
        except buttons.Refused as exc:
            return problem(request, exc.status, exc.message)
        except ArrError as exc:
            return problem(request, 502, f"Not done: {exc}")
        finally:
            live_cache.pop(library, None)
        return RedirectResponse(f"/#{library}-{int(id)}", status_code=303)

    # --- runs ---
    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request, library: str = "", page_no: int = Query(1, alias="page")):
        lib = library if library in LIBRARIES else ""
        count = db.count("SELECT count(*) FROM runs WHERE (? = '' OR library = ?)", lib, lib)
        pages = max(1, -(-count // RUNS_PAGE))
        page_no = min(max(page_no, 1), pages)
        rows = db.all(
            "SELECT * FROM runs WHERE (? = '' OR library = ?)"
            " ORDER BY generated_at DESC, library LIMIT ? OFFSET ?",
            lib,
            lib,
            RUNS_PAGE,
            (page_no - 1) * RUNS_PAGE,
        )
        return page(
            request,
            "runs.html",
            nav="runs",
            runs=rows,
            library=lib,
            count=count,
            page_no=page_no,
            pages=pages,
        )

    def run_row(library: str, run_key: str):
        if library not in LIBRARIES:
            raise HTTPException(404, "run not found")
        row = db.one("SELECT * FROM runs WHERE library = ? AND run_key = ?", library, run_key[:200])
        if not row:
            raise HTTPException(404, "run not found")
        return row

    @app.get("/run/{library}/{run_key}", response_class=HTMLResponse)
    def run_page(request: Request, library: str, run_key: str):
        row = run_row(library, run_key)
        summary = json.loads(row["summary"])
        groups = {k: v for k, v in summary.items() if isinstance(v, dict) and k != "digest"}
        singles = {k: v for k, v in summary.items() if not isinstance(v, dict) and k != "digest"}
        deleted = db.all(
            "SELECT * FROM actions WHERE library = ? AND run_key = ? ORDER BY size_gb DESC",
            library,
            row["run_key"],
        )
        return page(
            request,
            "run.html",
            nav="runs",
            run=row,
            blocks=json.loads(row["blocks"]),
            funnel=groups.pop("plan", {}),
            # figures already shown under fixed names are not listed a second time
            judge={k: v for k, v in groups.pop("judge", {}).items() if k not in SHOWN_JUDGE},
            executor=groups.pop("executor", {}),
            groups=groups,
            singles=singles,
            deleted=deleted,
        )

    @app.get("/run/{library}/{run_key}/document")
    def run_document(library: str, run_key: str):
        """The document exactly as the engine sent it."""
        row = run_row(library, run_key)
        doc = db.one("SELECT body FROM documents WHERE id = ?", row["document_id"])
        return Response(
            doc["body"] if doc else "{}",
            media_type="application/json",
            headers={"Content-Disposition": "inline"},
        )

    # --- one title ---
    @app.get("/title/{library}/{item_id}", response_class=HTMLResponse)
    def title_page(request: Request, library: str, item_id: int):
        if library not in LIBRARIES:
            raise HTTPException(404, "title not found")
        t = db.one("SELECT * FROM titles WHERE library = ? AND id = ?", library, item_id)
        c = db.one("SELECT * FROM catalogue WHERE library = ? AND id = ?", library, item_id)
        if not t and not c:
            raise HTTPException(404, "title not found")
        now_tags = live_or_none(library) if c else None
        cat = overlay(allow.decode(c), now_tags) if c else None
        latest = db.latest(library)
        on_list = find(latest[1], item_id) if latest else None
        known = db.one("SELECT imdb_id FROM links WHERE library = ? AND id = ?", library, item_id)
        return page(
            request,
            "title.html",
            nav="",
            library=library,
            item_id=item_id,
            heading=(t or c)["title"],  # type: ignore[index]
            year=(t or c)["year"],  # type: ignore[index]
            t=dict(t) if t else None,
            cat=cat,
            why=allow.reasons(library, cat) if cat else [],
            on_list=on_list[0] if on_list else "",
            verdicts=db.all(
                "SELECT * FROM verdicts WHERE library = ? AND item_id = ? ORDER BY id DESC",
                library,
                item_id,
            ),
            actions=db.all(
                "SELECT * FROM actions WHERE library = ? AND item_id = ? ORDER BY at DESC",
                library,
                item_id,
            ),
            taps=db.all(
                "SELECT * FROM taps WHERE library = ? AND item_id = ? ORDER BY id DESC",
                library,
                item_id,
            ),
            link=service_link(s, library, cat["slug"] if cat else ""),
            out=imdb_link(known["imdb_id"] if known else "", (t or c)["title"], (t or c)["year"]),  # type: ignore[index]
            tags_live=now_tags is not None,
        )

    # --- posters ---
    @app.get("/poster/{library}/{item_id}")
    def poster(library: str, item_id: int, w: int = POSTER_WIDTHS[0]):
        """One title's poster, from this app's own address so the page loads nothing from
        elsewhere. Fetched from the service once and then kept, so a title that has left the
        library still shows the poster it had. No poster: the placeholder."""
        if library not in LIBRARIES or not 0 < item_id < 10**15:
            raise HTTPException(404, "poster not found")
        width = w if w in POSTER_WIDTHS else POSTER_WIDTHS[0]
        folder = s.data_dir / "posters" / library

        def kept(size: int) -> tuple[bytes, str]:
            try:
                data = (folder / f"{item_id}-{size}").read_bytes()
            except OSError:
                data = b""
            return data, image_type(data[:16])

        data, kind = kept(width)
        cache = f"private, max-age={POSTER_SECONDS}"
        if not kind:
            wait = NO_POSTER_SECONDS
            try:
                found = clients[library].poster(item_id, width)
            except ArrError:
                found, wait = None, 0  # no answer is not "no poster": ask again on the next view
            if found:
                data, kind = found
                keep_file(folder / f"{item_id}-{width}", data)
            else:
                # Gone from the service, or no answer: the other width, kept earlier, still shows it.
                cache = f"private, max-age={wait}" if wait else "no-store"
                data, kind = next((p for p in map(kept, POSTER_WIDTHS) if p[1]), (b"", ""))
        if not kind:
            return RedirectResponse(NO_POSTER, status_code=302, headers={"Cache-Control": cache})
        return Response(data, media_type=kind, headers={"Cache-Control": cache})

    # --- allow list ---
    @app.get("/allow", response_class=HTMLResponse)
    def allow_page(request: Request, library: str = "", q: str = "", why: str = "", show: str = ""):
        lib = library if library in LIBRARIES else ""
        why = why if why in allow.WHY else ""
        adding = show == "all"  # the Add view: a search of the whole library, on the list or not
        q = q.strip()[:100]
        sql = "SELECT * FROM catalogue WHERE (? = '' OR library = ?)"
        args: list[Any] = [lib, lib]
        if q:
            sql += " AND title LIKE ? ESCAPE '\\'"
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.append(f"%{esc}%")
        rows = []
        now_tags = {lib_: live_or_none(lib_) for lib_ in LIBRARIES}
        ids = {lib_: imdb_ids(lib_) for lib_ in LIBRARIES}
        # The Add view lists nothing until it is searched: the whole library is not a list to read.
        for r in db.all(sql + " ORDER BY title COLLATE NOCASE", *args) if q or not adding else ():
            row = overlay(allow.decode(r), now_tags[r["library"]])
            row["why"] = allow.reasons(row["library"], row)
            row["imdb_id"] = ids[row["library"]].get(row["id"], "")
            row["mine"] = TAG_KEEP in row["tags"]  # added by hand, so this app can take it off
            if (adding or row["why"]) and (not why or why in row["why"]):
                rows.append(row)
        if not adding:
            rows.sort(key=lambda row: not row["mine"])  # stable: by title within each half
        counts = {
            lib_: db.count("SELECT count(*) FROM catalogue WHERE library = ?", lib_)
            for lib_ in LIBRARIES
        }
        return page(
            request,
            "allow.html",
            nav="allow",
            rows=rows[:ALLOW_PAGE],
            total=len(rows),
            by_hand=sum(row["mine"] for row in rows),
            cap=ALLOW_PAGE,
            library=lib,
            q=q,
            why=why,
            adding=adding,
            whys=allow.WHY,
            counts=counts,
            refreshed={lib_: db.get(f"allow_refresh:{lib_}", {}) for lib_ in LIBRARIES},
            unread=[lib_ for lib_ in LIBRARIES if now_tags[lib_] is None and counts[lib_]],
        )

    @app.post("/allow/act")
    def allow_act(
        request: Request,
        library: str = Form(""),
        id: str = Form(""),
        action: str = Form(""),
        back: str = Form("/allow"),
    ):
        if library not in LIBRARIES:
            return problem(request, 400, "That request names a library this app does not have.")
        if not (id.isascii() and id.isdigit() and len(id) <= 15):
            return problem(request, 400, "That request names no title.")
        try:
            buttons.allow_list(db, clients[library], library, int(id), action)
        except buttons.Refused as exc:
            return problem(request, exc.status, exc.message)
        except ArrError as exc:
            return problem(request, 502, f"Not done: {exc}")
        finally:
            live_cache.pop(library, None)
        # Back to the same filtered view; only a path on this site, never another host.
        target = (
            back
            if back.startswith("/allow") and "//" not in back and "\\" not in back
            else "/allow"
        )
        return RedirectResponse(target, status_code=303)

    @app.post("/allow/refresh")
    def allow_refresh(request: Request, back: str = Form("/ops")):
        if not refresh_all():
            return problem(request, 409, "The allow list is already being read. Try again shortly.")
        return RedirectResponse("/allow" if back == "/allow" else "/ops", status_code=303)

    # --- deleted ---
    @app.get("/deleted", response_class=HTMLResponse)
    def deleted(request: Request, library: str = "", page_no: int = Query(1, alias="page")):
        lib = library if library in LIBRARIES else ""
        now = dt.datetime.now(dt.timezone.utc)
        where = "WHERE (? = '' OR a.library = ?)"
        count = db.count(f"SELECT count(*) FROM actions a {where}", lib, lib)  # nosec B608
        pages = max(1, -(-count // DELETED_PAGE))
        page_no = min(max(page_no, 1), pages)
        rows = []
        for r in db.all(
            "SELECT a.*, c.slug FROM actions a LEFT JOIN catalogue c"  # nosec B608
            f" ON c.library = a.library AND c.id = a.item_id {where}"
            " ORDER BY a.size_gb DESC, a.at DESC, a.id DESC LIMIT ? OFFSET ?",
            lib,
            lib,
            DELETED_PAGE,
            (page_no - 1) * DELETED_PAGE,
        ):
            row = dict(r)
            at = moment(row["at"])
            until = at + dt.timedelta(days=s.recycle_days) if at else None
            row["bin_until"] = until.isoformat() if until else ""
            row["in_bin"] = bool(until and until > now and row["kind"] == "deleted")
            rows.append(row)
        sums = db.one(
            "SELECT coalesce(sum(size_gb), 0) AS gb, count(*) AS n FROM actions a"  # nosec B608
            f" {where} AND a.kind = 'deleted'",
            lib,
            lib,
        )
        return page(
            request,
            "deleted.html",
            nav="deleted",
            rows=rows,
            library=lib,
            total=sums["gb"] if sums else 0,
            deleted_n=sums["n"] if sums else 0,
            count=count,
            page_no=page_no,
            pages=pages,
            recycle_days=s.recycle_days,
            links={lib_: imdb_ids(lib_) for lib_ in LIBRARIES},
        )

    # --- ops ---
    @app.get("/ops", response_class=HTMLResponse)
    def ops(request: Request):
        libs = []
        for lib in LIBRARIES:
            last = db.one(
                "SELECT d.id, d.version, d.received_at, d.generated_at, d.size, r.run_key"
                " FROM documents d LEFT JOIN runs r ON r.document_id = d.id"
                " WHERE d.library = ? ORDER BY d.id DESC LIMIT 1",
                lib,
            )
            count = db.count("SELECT count(*) FROM documents WHERE library = ?", lib)
            held: dict[str, int] = {}
            for r in db.all("SELECT tags FROM catalogue WHERE library = ?", lib):
                for t in json.loads(r["tags"]):
                    if t in ("keep", "keep-review", TAG_KEEP, TAG_DELETE):
                        held[t] = held.get(t, 0) + 1
            now_tags = live_or_none(lib)  # fresh, and reconciles taps under the lock
            tags_live = {k: len(v) for k, v in now_tags.items()} if now_tags is not None else None
            if not clients[lib].configured:
                error = f"{SERVICE[lib]} is not configured"
            else:
                error = "" if now_tags is not None else f"{SERVICE[lib]} could not be read"
            libs.append(
                {
                    "library": lib,
                    "last": last,
                    "count": count,
                    "held": held,
                    "live": tags_live,
                    "error": error,
                    "key": bool(s.key(lib)),
                    "refresh": db.get(f"allow_refresh:{lib}", {}),
                }
            )
        rejects = db.all("SELECT * FROM rejects ORDER BY id DESC LIMIT 30")
        return page(
            request,
            "ops.html",
            nav="ops",
            libs=libs,
            rejects=rejects,
            schedule=s.allow_schedule,
            public_url=s.public_url,
            running=refreshing.locked(),
        )

    return app
