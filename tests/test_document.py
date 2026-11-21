"""Document validation: what is dropped, cut, or refused whole."""

import pytest
from conftest import sample
from curator.document import Invalid, find, judge, normalise, sign, summary


def test_unknown_fields_are_dropped_and_text_is_cut():
    doc = sample("film-v1")
    doc["going"][0]["poster"] = "https://example.invalid/p.jpg"
    doc["going"][0]["reason"] = "r" * 5000
    doc["extra"] = {"anything": 1}
    out = normalise("film", doc)
    assert "poster" not in out["going"][0] and "extra" not in out
    assert len(out["going"][0]["reason"]) == 600
    assert not out["summary"]  # version 1 has none, whatever it carries


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "live"},
        {"cap": -1},
        {"cap": 2.5},
        {"generated_at": "last tuesday"},
        {"generated_at": None},
        {"blocks": "not a list"},
        {"going": {"id": 1}},
        {"kept": [{"id": 0}]},
        {"kept": [{"id": True}]},
        {"later": [{"id": 9, "size_gb": float("inf")}]},
        {"later": [{"id": 9, "size_gb": -3}]},
        {"later": [{"id": 9, "title": 12}]},
        {"later": [{"id": 9, "actions": ["delete", "purge"]}]},
        {"later": [{"id": 9, "due_at": "soon"}]},
    ],
)
def test_malformed_documents_are_refused_whole(change):
    with pytest.raises(Invalid):
        normalise("film", {**sample("film-v1"), **change})


def test_deleted_entries_never_carry_actions_and_find_skips_them():
    doc = sample("film-v1")
    doc["deleted"][0]["actions"] = ["keep"]
    out = normalise("film", doc)
    assert out["deleted"][0]["actions"] == []
    assert find(out, 108) is None
    assert find(out, 106)[0] == "needs_you"


def test_summary_keeps_what_it_can_read():
    raw = {
        "plan": {"titles": 10, "nested": {"deep": {"too": 1}, "ok": 2}},
        "judge": {
            "status": "partial",
            "usage": {"input_tokens": 1000, "output_tokens": 50},
            "model": "claude-opus",
        },
        "digest": "  text  ",
        "count": float("nan"),
        "list": [1, 2],
    }
    out = summary(raw)
    assert out["plan"] == {"titles": 10, "nested.ok": 2}
    assert out["digest"] == "text" and "count" not in out and "list" not in out
    assert judge(out) == {
        "status": "partial",
        "batches": None,
        "failed": None,
        "tokens_in": 1000,
        "tokens_out": 50,
        "model": "claude-opus",
    }
    assert not summary("not an object")


def test_signature_matches_the_engine_recipe():
    import hashlib
    import hmac

    body = b'{"a":1}'
    want = "sha256=" + hmac.new(b"k", b"film\n" + body, hashlib.sha256).hexdigest()
    assert sign("k", "film", body) == want
