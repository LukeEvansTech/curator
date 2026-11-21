# Product

## Platform

web

## Users

One person who runs a home media library in Radarr and Sonarr. The main moment is on a phone: the
weekly cleanup's push notification arrives; one tap opens This week, a flick through what goes at
the next run with the reason for each, and one or two titles get kept. Browsing past runs, the
allow list and what was deleted is occasional.

## Product Purpose

The cleanup plans, an LLM judge proposes keep or delete with a reason, and the executor acts after a
one-week window. The judge's keep holds a title for a year, then it is judged again; keeping it for
good is a person's decision (Keep forever) or the rating bar's. Curator is where that becomes visible and overrulable: every run and verdict is
kept, and a person can veto the judge with one tap in either direction. Doing nothing means the
cleanup proceeds. Success: nothing goes that the owner would have kept, and the run history is
never invisible again.

## Capabilities and Constraints

- Pages: This week, Runs and one run, one title, Allow list, Deleted, Ops.
- Writes only two tags (`cleanup-keep`, `cleanup-eligible`) through the services' APIs.
- Server-rendered FastAPI and Jinja, one CSS file, no script.
- Titles, synopses and reasons come from third-party databases and a model: untrusted text.

## Product Principles

1. The reason comes with every title: no verdict without its why.
2. One tap to overrule, one tap to undo.
3. Say plainly when something is stale, blocked or unreadable.
