"""Pages and the publish endpoint. The engines' push notification opens `/`."""

from __future__ import annotations

import datetime as dt
import hmac
import json
import logging
import math
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

from . import allow, buttons, ingest
from .arr import TAG_DELETE, TAG_KEEP, ArrError, TagClient, radarr, sonarr
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
POLICY = (
    "default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


def gb(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(f):
        return ""
    return f"{f / 1000:,.2f} TB" if f >= 1000 else f"{f:,.1f} GB"


def facts(item: dict) -> str:
    """Size, rating, plays and origin on one line; a fact the document lacks is left out."""
    parts = []
    if item.get("size_gb") is not None:
        parts.append(gb(item["size_gb"]))
    if item.get("imdb_score") is not None:
        votes = (
            f" on {int(item['imdb_votes']):,} votes" if item.get("imdb_votes") is not None else ""
        )
        parts.append(f"IMDb {float(item['imdb_score']):.1f}{votes}")
    if item.get("plays") is not None:
        n = int(item["plays"])
        parts.append(f"{n} play{'' if n == 1 else 's'}")
    if item.get("finished_by"):
        parts.append(f"finished by {int(item['finished_by'])}")
    origin = ORIGINS.get(item.get("origin") or "", "")
    if item.get("origin") == "requested" and item.get("requested_by"):
        origin = f"requested by {item['requested_by']}"
    if origin:
        parts.append(origin)
    return " · ".join(parts)


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
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            host = urllib.parse.urlsplit(origin).netloc
            if (site and site not in ("same-origin", "none")) or (
                origin and host != request.headers.get("host", "")
            ):
                return PlainTextResponse("cross-site request refused", status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
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

    tpl.env.filters.update(gb=gb, when=when, day=day, ago=ago, facts=facts, total_gb=total_gb)
    tpl.env.globals.update(
        version=s.version, names=NAMES, service=SERVICE, byline=BYLINE, cost=cost
    )

    def page(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
        return tpl.TemplateResponse(request, name, ctx, status_code=status)

    def problem(request: Request, status: int, message: str) -> HTMLResponse:
        return page(request, "problem.html", status=status, status_code=status, message=message)

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
    def week_view(library: str) -> dict:
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
        ids = [i["id"] for k in ("going", "later", "kept", "needs_you") for i in doc.get(k) or []]
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
            taps = {}
            for item_id in ids:
                if reconciled and view["live"] is not None:
                    taps[item_id] = buttons.settle(db, library, item_id, view["live"])
                else:
                    taps[item_id] = buttons.open_tap(db, library, item_id)
        finally:
            if reconciled:
                ingest.LOCK.release()
        mine, pending = {}, set()
        for item_id, tap in taps.items():
            if tap and not tap["confirmed"]:
                pending.add(item_id)
            elif tap:
                mine[item_id] = tap["tag"]
        view["mine"], view["pending"] = mine, pending
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
            out["buttons"].append(("keep", "Keep"))
        if "delete" in item["actions"] and not has_del:
            out["buttons"].append(("delete", "Delete instead"))
        return out

    tpl.env.globals["card_state"] = card_state

    @app.get("/", response_class=HTMLResponse)
    def week(request: Request):
        return page(request, "week.html", nav="week", views=[week_view(lib) for lib in LIBRARIES])

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
        cat = allow.decode(c) if c else None
        latest = db.latest(library)
        on_list = find(latest[1], item_id) if latest else None
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
        )

    # --- allow list ---
    @app.get("/allow", response_class=HTMLResponse)
    def allow_page(request: Request, library: str = "", q: str = "", why: str = "", show: str = ""):
        lib = library if library in LIBRARIES else ""
        why = why if why in allow.WHY else ""
        everything = show == "all"
        q = q.strip()[:100]
        sql = "SELECT * FROM catalogue WHERE (? = '' OR library = ?)"
        args: list[Any] = [lib, lib]
        if q:
            sql += " AND title LIKE ? ESCAPE '\\'"
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.append(f"%{esc}%")
        rows = []
        for r in db.all(sql + " ORDER BY title COLLATE NOCASE", *args):
            row = allow.decode(r)
            row["why"] = allow.reasons(row["library"], row)
            if (everything or row["why"]) and (not why or why in row["why"]):
                rows.append(row)
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
            cap=ALLOW_PAGE,
            library=lib,
            q=q,
            why=why,
            show="all" if everything else "",
            whys=allow.WHY,
            counts=counts,
            refreshed={lib_: db.get(f"allow_refresh:{lib_}", {}) for lib_ in LIBRARIES},
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
            tags_live, error = None, ""
            if clients[lib].configured:
                try:
                    tags_live = {k: len(v) for k, v in live(lib).items()}
                except ArrError as exc:
                    error = str(exc)
            else:
                error = f"{SERVICE[lib]} is not configured"
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
