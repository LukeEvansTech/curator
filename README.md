# curator

Review pages for a weekly Radarr and Sonarr library cleanup. An engine plans, an LLM judge proposes
keep or delete with a reason, and an executor acts after a one-week review window. A title the
judge keeps is held for a year and then judged again; only a person (Keep forever) or the rating bar
keeps a title for good. Curator keeps every run and verdict the engines publish, shows what goes at
the next run, and lets a person overrule the judge with one tap.

## How it works

```text
engine report step ─▶ POST /publish/{film|tv}   signed JSON review document (version 1 or 2)
                   ─▶ kept as received, then read into runs, titles, verdicts and actions
This week (/)      ─▶ Keep, Keep forever: cleanup-keep; Delete instead: cleanup-eligible; Undo removes it
Allow list         ─▶ read from Radarr and Sonarr every 6 hours; add or remove cleanup-keep
next weekly run    ─▶ the engine's plan reads the tags
```

- **Publishing.** `X-Curator-Signature: sha256=<hex>` is HMAC-SHA256 over `library + "\n" + body`,
  keyed with that library's API key (Radarr's for `film`, Sonarr's for `tv`). An unknown library,
  a bad signature, a body over the size cap, an unknown `version` or a malformed document is
  refused and listed on Ops. A document older than the one held is refused; a retry of one already
  held answers 200.
- **Writes.** Only the two tags above, only through the services' APIs. It never deletes, never
  touches `keep`, and never writes to the engines' state.

## Pages

| Page                            | Shows                                                                                 |
| ------------------------------- | ------------------------------------------------------------------------------------- |
| `/`                             | Per library: totals, Going, Needs you, Later, Being kept for a year, Deleted last run |
| `/review`                       | One title at a time: Keep, let it go or decide later, until the list is answered      |
| `/runs`, `/run/{library}/{run}` | Every run: counts, plan funnel, judge batches, tokens and cost, digest, JSON          |
| `/title/{library}/{id}`         | One title: facts, every verdict, what was done, button presses, service link          |
| `/allow`                        | Protected titles and why (rating bar, `keep`, `cleanup-keep`), searchable             |
| `/deleted`                      | What went, largest first, and what a restore needs                                    |
| `/ops`                          | Last document per library, refused publishes, tags in the services, refresh           |
| `/poster/{library}/{id}`        | A title's poster, fetched from its service once and kept; `?w=500` is the large one   |
| `/health`, `/metrics`           | Liveness; `curator_last_document_timestamp_seconds{library}` and more                 |

## Run it

```sh
uv venv -p 3.15 .venv && uv pip install -p .venv -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q
CURATOR_DATA=data .venv/bin/python -m curator import film tests/data/film-v1.json
CURATOR_DATA=data .venv/bin/python -m curator import tv tests/data/tv-v2.json
CURATOR_DATA=data .venv/bin/python -m curator serve   # http://localhost:8080
```

Every title shows its poster and links to its IMDb page; both open IMDb in a new tab. Without
Radarr and Sonarr configured the pages render, say the live tags could not be read, and show a
placeholder for every poster.

Docker: `docker build -t curator .` then
`docker run --read-only -v curator:/data -p 8080:8080 curator`. The image runs as uid 65532.

## Configuration

| Variable                     | Default         | Description                                                           |
| ---------------------------- | --------------- | --------------------------------------------------------------------- |
| `CURATOR_DATA`               | `data`          | Directory for `curator.db`.                                           |
| `RADARR_URL`                 | none            | Radarr API base URL.                                                  |
| `RADARR_API_KEY`             | none            | Radarr API key; also the key `film` documents are signed with.        |
| `SONARR_URL`                 | none            | Sonarr API base URL.                                                  |
| `SONARR_API_KEY`             | none            | Sonarr API key; also the key `tv` documents are signed with.          |
| `RADARR_LINK_URL`            | none            | Radarr web UI base URL for "Open in Radarr" links. Empty: no link.    |
| `SONARR_LINK_URL`            | none            | Sonarr web UI base URL for "Open in Sonarr" links. Empty: no link.    |
| `CURATOR_PUBLIC_URL`         | none            | Base URL of the pages.                                                |
| `CURATOR_TZ`                 | `Europe/London` | Timezone for dates on the pages and the refresh cron.                 |
| `CURATOR_ALLOW_SCHEDULE`     | `23 */6 * * *`  | Cron for reading the allow list from the services.                    |
| `CURATOR_RECYCLE_DAYS`       | `14`            | Days a deleted file stays in the services' recycle bin.               |
| `CURATOR_STALE_DAYS`         | `8`             | A document this many days old is shown as stale.                      |
| `CURATOR_PRICE_IN_PER_MTOK`  | `4.0`           | Judge input price, US dollars per million tokens, for cost estimates. |
| `CURATOR_PRICE_OUT_PER_MTOK` | `20.0`          | Judge output price, US dollars per million tokens.                    |
| `CURATOR_MAX_PUBLISH_BYTES`  | `16777216`      | Largest document accepted.                                            |
| `PORT`                       | `8080`          | Listen port.                                                          |
