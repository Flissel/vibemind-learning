"""Source freshness from the command line (inside the learning-api container).

    python -m spaces.learning.services.quellen.cli aufnehmen --course <id> --url <url>
    python -m spaces.learning.services.quellen.cli pruefen

``aufnehmen`` watches a page for a course with the digest of its visible text now;
``pruefen`` (the nightly run) fetches every watched page and records the changed ones.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

from spaces.learning.services.db.session import build_session_factory, create_learning_engine
from spaces.learning.services.quellen.frische import check, text_digest
from spaces.learning.services.quellen.store import WatchStore

_TIMEOUT_S = 30.0
_MAX_BYTES = 5_000_000


def fetch(url: str) -> str:
    response = httpx.get(url, timeout=_TIMEOUT_S, follow_redirects=True, headers={"User-Agent": "VibeMind-Learning-Quellenpruefung/1"})
    response.raise_for_status()
    if len(response.content) > _MAX_BYTES:
        raise ValueError("page is too large")
    return response.text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quellen")
    sub = parser.add_subparsers(dest="command", required=True)
    take = sub.add_parser("aufnehmen", help="watch a source page for a course")
    take.add_argument("--course", required=True)
    take.add_argument("--url", required=True)
    sub.add_parser("pruefen", help="check every watched page and record changes")
    args = parser.parse_args(argv)

    store = WatchStore(build_session_factory(create_learning_engine(os.environ["LEARNING_DATABASE_URL"])))
    if args.command == "aufnehmen":
        if not args.url.startswith(("http://", "https://")):
            raise SystemExit("url must be http(s)")
        watch = store.take_up(course_id=args.course, url=args.url, sha256=text_digest(fetch(args.url)))
        print(f"{watch.url}: aufgenommen fuer Kurs {watch.course_id} ({watch.sha256[:12]}…)")
        return 0
    changes = check(store.watches(), fetch)
    store.record(changes)
    for change in changes:
        print(f"GEAENDERT {change.course_id} {change.url}")
    print(f"{len(changes)} geaenderte Quelle(n)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
