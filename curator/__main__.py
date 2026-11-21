"""curator command line.

python -m curator serve                     # pages and /publish on :8080, allow list read on schedule
python -m curator import film document.json # keep a review document without a signed publish
python -m curator refresh                   # read the allow list from Radarr and Sonarr now
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import allow, config, ingest
from .db import DB
from .document import LIBRARIES, Invalid, normalise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="curator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    im = sub.add_parser("import", help="keep a review document from a file (local use)")
    im.add_argument("library", choices=LIBRARIES)
    im.add_argument("path")
    sub.add_parser("refresh")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = config.load()
    db = DB(s.db_path)

    if a.cmd == "serve":
        import uvicorn

        from .web import create_app

        app = create_app(s, db, start_scheduler=True)
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))  # nosec B104
        return 0
    if a.cmd == "import":
        raw = Path(a.path).read_bytes()
        try:
            doc = normalise(a.library, json.loads(raw))
            doc_id, new = ingest.accept(db, a.library, raw, doc)
        except (Invalid, ingest.Stale, json.JSONDecodeError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        print(f"document {doc_id} {'kept' if new else 'already held'}")
        return 0
    if a.cmd == "refresh":
        from .web import default_clients

        clients = default_clients(s)
        for lib in LIBRARIES:
            print(lib, allow.refresh(db, lib, clients[lib]))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
