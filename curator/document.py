"""Validation of a published review document.

Everything in a document reaches a browser, and the ids in it are what the buttons may act on, so
nothing is taken on trust: unknown fields are dropped, every value is checked for type and cut to
length, and a malformed document is refused whole rather than shown in part. The one exception is
version 2's `summary`, whose shape is still settling on the engine side: anything in it that does
not fit is left out, never a reason to refuse the titles around it.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
from datetime import datetime, timezone
from typing import Any

VERSIONS = (1, 2)
LIBRARIES = ("film", "tv")
ACTIONS = ("keep", "delete")
# Sections a button can act on, then the one that is only a record of what happened.
ACTIONABLE = ("going", "later", "kept", "needs_you")
SECTIONS = (*ACTIONABLE, "deleted")

MAX_ITEMS = 20000
MAX_BLOCKS = 50
TEXT_LIMITS = {
    "title": 300,
    "origin": 40,
    "requested_by": 80,
    "overview": 400,
    "verdict": 20,
    "by": 20,
    "reason": 600,
}
NUMBERS = ("year", "size_gb", "imdb_score", "imdb_votes", "plays", "finished_by")
STAMPS = ("proposed_at", "due_at")

SUMMARY_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
SUMMARY_KEYS = 40  # top-level keys kept from a summary
SUMMARY_FIELDS = 80  # fields kept from one summary group
SUMMARY_TEXT = 600
DIGEST_TEXT = 20000


def sign(key: str, library: str, body: bytes) -> str:
    """The signature a document must carry: HMAC-SHA256 over the library name and the body, keyed
    with that library's API key (the engine's `review.sign`)."""
    digest = hmac.new(key.encode(), library.encode() + b"\n" + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class Invalid(ValueError):
    """The document is not one this app will keep, show or act on."""


def moment(value: Any) -> datetime | None:
    """Parse a timestamp into an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _stamp(value: Any, name: str, required: bool = False) -> str:
    parsed = moment(value)
    if parsed is None:
        if required or value not in (None, ""):
            raise Invalid(f"{name} is not a timestamp")
        return ""
    return parsed.isoformat()


def _text(value: Any, limit: int) -> str:
    """A string cut to its limit; None reads as empty, any other type is refused."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise Invalid("a text field holds something that is not text")
    return value.strip()[:limit]


def _number(value: Any, name: str) -> int | float | None:
    """A finite, non-negative number, or None."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Invalid(f"{name} is not a usable number")
    if not 0 <= value <= 1e15 or not math.isfinite(value):  # bound first: see _scalar
        raise Invalid(f"{name} is not a usable number")
    return value


def _list(value: Any, name: str) -> list:
    """A list, or empty when absent or null. Any other value is refused: read as empty, a
    malformed `{}` or `false` would show "nothing is due" in place of the titles it should hold."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise Invalid(f"{name} is not a list")
    return value


def _item(raw: Any, section: str) -> dict[str, Any]:
    """One title, reduced to the fields the pages show."""
    if not isinstance(raw, dict):
        raise Invalid(f"an entry in {section} is not an object")
    item_id = raw.get("id")
    if not isinstance(item_id, int) or isinstance(item_id, bool) or not 0 < item_id < 2**53:
        raise Invalid(f"an entry in {section} has no usable id")
    actions = _list(raw.get("actions"), f"actions of entry {item_id} in {section}")
    if any(a not in ACTIONS for a in actions):
        raise Invalid(f"entry {item_id} in {section} names an action this app does not have")
    item: dict[str, Any] = {"id": item_id}
    for name, limit in TEXT_LIMITS.items():
        item[name] = _text(raw.get(name), limit)
    for name in NUMBERS:
        item[name] = _number(raw.get(name), name)
    for name in STAMPS:
        item[name] = _stamp(raw.get(name), name)
    item["title"] = item["title"] or f"#{item_id}"
    item["simulated"] = raw.get("simulated") is True
    # Nothing can be done from the page about a title that has already gone.
    item["actions"] = [] if section == "deleted" else sorted(set(actions))
    return item


def _scalar(value: Any) -> str | int | float | bool | None:
    """A summary value the pages can show, or None when it is not one."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        # bound first: math.isfinite overflows on a huge JSON integer
        return value if abs(value) <= 1e15 and math.isfinite(value) else None
    if isinstance(value, str):
        return value.strip()[:SUMMARY_TEXT]
    return None


def _flat(group: dict, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    """A summary group as dotted keys to scalars, two levels deep; anything else is left out."""
    out: dict[str, Any] = {}
    for key, value in group.items():
        if len(out) >= SUMMARY_FIELDS or not isinstance(key, str) or not SUMMARY_KEY.match(key):
            continue
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            if depth < 1:
                for k, v in _flat(value, f"{name}.", depth + 1).items():
                    if len(out) < SUMMARY_FIELDS:
                        out[k] = v
            continue
        scalar = _scalar(value)
        if scalar is not None:
            out[name] = scalar
    return out


def _digest(value: Any) -> dict[str, str]:
    """The digest as sent: the engines' `{title, message, text}`, or plain text."""
    if isinstance(value, str):
        value = {"text": value}
    if not isinstance(value, dict):
        return {}
    out = {}
    for name, limit in (("title", SUMMARY_TEXT), ("message", SUMMARY_TEXT), ("text", DIGEST_TEXT)):
        v = value.get(name)
        if isinstance(v, str) and v.strip():
            out[name] = v.strip()[:limit]
    return out


def summary(raw: Any) -> dict[str, Any]:
    """Version 2's optional summary, kept as far as it is readable.

    `digest` is the run's digest as sent (title, message, text). Every other key holds one value
    or a group of values, flattened two levels deep (the plan's funnel, with its `counts`; the
    judge's status, batches, tokens and model group; the executor's totals).
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if len(out) >= SUMMARY_KEYS or not isinstance(key, str) or not SUMMARY_KEY.match(key):
            continue
        if key == "digest":
            digest = _digest(value)
            if digest:
                out["digest"] = digest
        elif isinstance(value, dict):
            group = _flat(value)
            if group:
                out[key] = group
        else:
            scalar = _scalar(value)
            if scalar is not None:
                out[key] = scalar
    return out


def normalise(library: str, payload: Any) -> dict[str, Any]:
    """Check a published document and return the copy the pages read. Raises `Invalid`."""
    if library not in LIBRARIES:
        raise Invalid("unknown library")
    if not isinstance(payload, dict):
        raise Invalid("the document is not an object")
    version = payload.get("version")
    if isinstance(version, bool) or version not in VERSIONS:
        raise Invalid(f"version {str(version)[:20]!r} is not one this app reads")
    if payload.get("library") != library:
        raise Invalid("the document names a different library from the one it was sent to")
    if payload.get("mode") not in ("dry-run", "act"):
        raise Invalid("mode is neither dry-run nor act")
    cap = payload.get("cap")
    if not isinstance(cap, int) or isinstance(cap, bool) or not 0 <= cap < 2**31:
        raise Invalid("cap is not a whole number")
    blocks = _list(payload.get("blocks"), "blocks")
    if len(blocks) > MAX_BLOCKS:
        raise Invalid("blocks is not a short list")

    document: dict[str, Any] = {
        "version": version,
        "library": library,
        "generated_at": _stamp(payload.get("generated_at"), "generated_at", required=True),
        "run_id": _text(payload.get("run_id"), 100),
        "mode": payload["mode"],
        "cap": cap,
        "next_run_at": _stamp(payload.get("next_run_at"), "next_run_at"),
        "blocks": [_text(block, 600) for block in blocks],
        "summary": summary(payload.get("summary")) if version >= 2 else {},
    }
    seen: set[int] = set()
    total = 0
    for section in SECTIONS:
        entries = _list(payload.get(section), section)
        total += len(entries)
        if total > MAX_ITEMS:
            raise Invalid("the document lists more titles than this app will hold")
        items = [_item(entry, section) for entry in entries]
        if section in ACTIONABLE:
            ids = [item["id"] for item in items]
            # One title, one answer: listed twice, a button would act on whichever came first.
            if len(set(ids)) != len(ids) or seen & set(ids):
                raise Invalid(f"a title is listed more than once (in {section})")
            seen.update(ids)
        document[section] = items
    return document


def find(document: dict | None, item_id: int) -> tuple[str, dict] | None:
    """The section and entry for a title a button can act on, or None."""
    for section in ACTIONABLE:
        for item in (document or {}).get(section) or []:
            if item["id"] == item_id:
                return section, item
    return None


# Names a version 2 summary might use for the judge's figures; the first one present wins.
JUDGE_FIELDS = {
    "status": ("status", "state", "result"),
    "batches": ("batches", "batches_total", "batch_count"),
    "failed": ("batches_failed", "failed_batches", "failed"),
    "tokens_in": ("input_tokens", "tokens_in", "usage.input_tokens", "tokens.input"),
    "tokens_out": ("output_tokens", "tokens_out", "usage.output_tokens", "tokens.output"),
    "model": ("model", "model_group", "group"),
}


def judge(summary_: dict) -> dict[str, Any]:
    """The judge's figures from a summary, under fixed names; a figure it lacks is None."""
    found = summary_.get("judge")
    group: dict[str, Any] = found if isinstance(found, dict) else {}
    out: dict[str, Any] = {}
    for name, candidates in JUDGE_FIELDS.items():
        out[name] = next((group[c] for c in candidates if c in group), None)
    for name in ("batches", "failed", "tokens_in", "tokens_out"):
        v = out[name]
        out[name] = int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    for name in ("status", "model"):
        out[name] = str(out[name]) if out[name] is not None else None
    return out
