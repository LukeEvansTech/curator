# curator agent guide

Review pages for the weekly Radarr and Sonarr cleanup. `README.md` covers what it does and how to run
it; this file holds what the code does not say.

- Phase 1: the engines (planner, judge, executor) still run elsewhere as scheduled jobs. Their
  `report` step POSTs a signed review document per library to `/publish/{library}`; this app keeps
  every one and draws the pages from them. The contract is the engine's `review.py` (`build`,
  `sign`, `publish`); `document.py` here is the validating side. Version 2 adds an optional
  `summary`; every field in it stays optional.
- Layout: `document.py` (validation, pure), `ingest.py` (document into tables), `buttons.py` (the
  only tag writes), `arr.py` (the only network code), `allow.py` (allow list), `web.py` and
  `templates/` (pages), `db.py` (schema).
- Deployed on the home cluster as `curator`, which alerts on
  `curator_last_document_timestamp_seconds` from `GET /metrics`. The cluster runs the image of the
  latest `v*` tag: a merge to `main` deploys nothing until a tag is pushed and the pin PR merges.
- Before pushing: `python -m pytest -q`, then black, isort, flake8, pylint, ruff and mypy with the
  configs in `.github/linters/` (super-linter runs the same).

## Review rules

- **Only two tags, only through the services' APIs.** `arr.TagClient` refuses any label but
  `cleanup-keep` and `cleanup-eligible`. Never add a write path that deletes, touches `keep`, or
  writes to the engines' state. The tags are the decision; the next plan reads them.
- **A This week button acts only on the latest document.** Only IDs it lists, only the `actions`
  it offers for that ID, and only with a form token drawn from that document.
- **Undo removes only what this app added.** A tap is recorded after the service confirms the tag
  (read back, since the editor endpoint acknowledges before acting). A tag already set elsewhere
  is left alone and never becomes undoable.
- **All document and service text is untrusted.** Autoescape stays on; never `| safe`,
  `Markup` or a raw string into HTML. The pages run no script and load nothing from elsewhere
  (the content security policy in `web.py` enforces it).
- **Publish checks run in this order:** known library, key configured, size, signature
  (constant time, before parsing), then validation. Every accepted document is kept as received;
  an older one never replaces a newer one.
