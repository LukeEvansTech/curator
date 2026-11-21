"""POST /publish/{library}: only a document its own engine signed is kept, and every one is kept."""

import json

from conftest import KEYS, body_of, publish, sample, signed
from curator.document import sign


def test_good_v1_document_is_kept_with_its_tables(env):
    _, db, client, *_ = env
    doc = sample("film-v1")
    r = publish(client, "film", doc)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "library": "film", "document": 1, "new": True, "titles": 7}
    stored = db.one("SELECT * FROM documents")
    assert stored["body"] == body_of(doc).decode() and stored["version"] == 1
    run = db.one("SELECT * FROM runs")
    assert (run["run_key"], run["going_n"], run["deleted_n"], run["mode"]) == (
        "run-20261115T080000-f1lm01",
        3,
        1,
        "act",
    )
    assert run["going_gb"] == 74.6 and run["summary"] == "{}" and run["tokens_in"] is None
    assert db.one("SELECT count(*) AS n FROM titles")["n"] == 8
    assert db.one("SELECT count(*) AS n FROM verdicts")["n"] == 7
    act = db.one("SELECT * FROM actions")
    assert (act["item_id"], act["kind"], act["size_gb"]) == (108, "deleted", 27.9)


def test_v2_summary_is_read_and_optional_fields_survive(env):
    _, db, client, *_ = env
    doc = sample("tv-v2")
    assert publish(client, "tv", doc).status_code == 200
    run = db.one("SELECT * FROM runs")
    assert (run["judge_status"], run["judge_batches"], run["judge_failed"]) == ("ok", 7, 1)
    assert (run["tokens_in"], run["tokens_out"], run["model"]) == (184000, 21500, "claude-opus")
    assert run["digest"].startswith("TV cleanup, dry run")
    assert json.loads(run["summary"])["plan"]["candidates"] == 98
    # a dry run's deletion is recorded as one it would have made
    assert db.one("SELECT kind FROM actions")["kind"] == "would-delete"


def test_v2_with_odd_or_missing_summary_is_still_accepted(env):
    _, db, client, *_ = env
    doc = sample("tv-v2")
    doc["summary"] = {
        "judge": {"status": ["not", "a", "scalar"], "usage": {"input_tokens": 5}},
        "plan": "a string, not counts",
        "bad key!": 1,
        "digest": 42,
    }
    assert publish(client, "tv", doc).status_code == 200
    run = db.one("SELECT * FROM runs")
    assert run["tokens_in"] == 5 and run["judge_status"] is None and run["digest"] == ""
    assert json.loads(run["summary"]) == {
        "judge": {"usage.input_tokens": 5},
        "plan": "a string, not counts",
    }
    del doc["summary"]
    doc["generated_at"] = "2026-11-16T08:00:00+00:00"
    assert publish(client, "tv", doc).status_code == 200


def test_bad_signature_wrong_key_and_missing_header_are_refused(env):
    _, db, client, *_ = env
    doc = sample("film-v1")
    assert publish(client, "film", doc, key="not-the-key").status_code == 401
    # signed with the other library's key: a document cannot be passed off as the other library's
    assert publish(client, "film", doc, key=KEYS["tv"]).status_code == 401
    body, headers = signed("film", doc)
    del headers["x-curator-signature"]
    assert client.post("/publish/film", content=body, headers=headers).status_code == 401
    # a body changed after signing
    body, headers = signed("film", doc)
    tampered = body.replace(b"Mega Gator", b"Mega Gnat!")
    assert client.post("/publish/film", content=tampered, headers=headers).status_code == 401
    assert db.one("SELECT count(*) AS n FROM documents")["n"] == 0
    assert db.one("SELECT count(*) AS n FROM rejects WHERE status = 401")["n"] == 4


def test_wrong_and_unknown_library(env):
    _, db, client, *_ = env
    doc = sample("film-v1")
    # signed correctly for tv, but the document says film
    body = body_of(doc)
    headers = {"x-curator-signature": sign(KEYS["tv"], "tv", body)}
    r = client.post("/publish/tv", content=body, headers=headers)
    assert r.status_code == 400 and "different library" in r.json()["error"]
    r = client.post("/publish/music", content=body, headers=headers)
    assert r.status_code == 404
    assert db.one("SELECT library FROM rejects ORDER BY id DESC LIMIT 1")["library"] == "unknown"


def test_unknown_version_and_malformed_documents(env):
    _, _, client, *_ = env
    for version in (3, 0, "1", True, None):
        doc = {**sample("film-v1"), "version": version}
        r = publish(client, "film", doc)
        assert r.status_code == 400, version
    bad = {**sample("film-v1"), "going": [{"id": "1"}]}
    assert publish(client, "film", bad).status_code == 400
    dup = sample("film-v1")
    dup["later"].append(dup["going"][0])
    assert "more than once" in publish(client, "film", dup).json()["error"]
    body = b"{not json"
    headers = {"x-curator-signature": sign(KEYS["film"], "film", body)}
    assert client.post("/publish/film", content=body, headers=headers).status_code == 400


def test_oversized_body_is_refused_before_it_is_read(env):
    s, db, client, *_ = env
    doc = sample("film-v1")
    doc["going"][0]["overview"] = "x" * 400
    body, headers = signed("film", doc)
    big = body + b" " * (s.max_publish_bytes - len(body) + 1)
    headers["x-curator-signature"] = sign(KEYS["film"], "film", big)
    r = client.post("/publish/film", content=big, headers=headers)
    assert r.status_code == 413
    assert db.one("SELECT count(*) AS n FROM documents")["n"] == 0


def test_no_key_configured_refuses_rather_than_accepts(env, monkeypatch):
    from curator import config
    from curator.web import create_app
    from fastapi.testclient import TestClient

    _, db, *_ = env
    monkeypatch.setenv("RADARR_API_KEY", "")
    app = TestClient(create_app(config.load(), db))
    body = body_of(sample("film-v1"))
    r = app.post(
        "/publish/film", content=body, headers={"x-curator-signature": sign("", "film", body)}
    )
    assert r.status_code == 503


def test_every_document_is_kept_retries_are_idempotent_and_old_ones_refused(env):
    _, db, client, *_ = env
    doc = sample("film-v1")
    assert publish(client, "film", doc).json()["new"] is True
    again = publish(client, "film", doc)
    assert again.status_code == 200 and again.json()["new"] is False
    newer = {
        **doc,
        "generated_at": "2026-11-22T08:00:00+00:00",
        "run_id": "run-20261122T080000-f1lm02",
    }
    assert publish(client, "film", newer).status_code == 200
    older = {**doc, "generated_at": "2026-11-01T08:00:00+00:00"}
    assert publish(client, "film", older).status_code == 409
    assert db.one("SELECT count(*) AS n FROM documents")["n"] == 2
    assert db.one("SELECT count(*) AS n FROM runs")["n"] == 2
    # the same verdict seen in two runs is one verdict row, extended
    v = db.one("SELECT * FROM verdicts WHERE item_id = 101")
    assert (v["first_run"], v["last_run"]) == (
        "run-20261115T080000-f1lm01",
        "run-20261122T080000-f1lm02",
    )
    assert db.one("SELECT count(*) AS n FROM verdicts WHERE item_id = 101")["n"] == 1


def test_a_run_id_that_is_not_url_safe_is_named_by_its_document(env):
    _, db, client, *_ = env
    doc = {**sample("film-v1"), "run_id": "../../etc"}
    assert publish(client, "film", doc).status_code == 200
    assert db.one("SELECT run_key FROM runs")["run_key"] == "doc-1"
    assert client.get("/run/film/doc-1").status_code == 200


def test_metrics_report_the_last_document_per_library(env):
    _, _, client, *_ = env
    m = client.get("/metrics").text
    assert 'curator_last_document_timestamp_seconds{library="film"} 0' in m
    publish(client, "film", sample("film-v1"))
    m = client.get("/metrics").text
    line = next(
        x
        for x in m.splitlines()
        if x.startswith('curator_last_document_timestamp_seconds{library="film"}')
    )
    assert int(line.split()[-1]) > 1_700_000_000
    assert 'curator_going_titles{library="film"} 3' in m
    assert 'curator_documents_total{library="tv"} 0' in m
