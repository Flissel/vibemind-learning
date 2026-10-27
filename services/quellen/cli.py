"""Source freshness from the command line (inside the learning-api container).

    python -m spaces.learning.services.quellen.cli aufnehmen --course <id> --url <url>
    python -m spaces.learning.services.quellen.cli pruefen

``aufnehmen`` watches a page for a course with the digest of its visible text now;
``pruefen`` (the nightly run) fetches every watched page and records the changed ones.

Fetching is defensive: only http(s) to public addresses (a watched page could redirect
into the VM's network otherwise), every redirect hop checked, bodies capped while they
stream.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import sys
from urllib.parse import urljoin, urlsplit

import httpx

from spaces.learning.services.db.session import build_session_factory, create_learning_engine
from spaces.learning.services.quellen.frische import check, text_digest
from spaces.learning.services.quellen.store import WatchStore

_TIMEOUT_S = 30.0
_MAX_BYTES = 5_000_000
_MAX_REDIRECTS = 5
_LOCAL_SUFFIXES = (".local", ".internal", ".localhost", ".lan")


def is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def resolve(host: str) -> str:
    """The address a host name resolves to (first answer); patched in tests."""
    return socket.gethostbyname(host)


def _checked(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("only public http(s) pages are fetched")
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        raise ValueError("host is not public")
    try:
        ipaddress.ip_address(host)
        address = host
    except ValueError:
        try:
            address = resolve(host)
        except OSError as error:
            raise ValueError("host does not resolve") from error
    if not is_public_address(address):
        raise ValueError("host is not public")
    return url


def _client() -> httpx.Client:
    return httpx.Client(timeout=_TIMEOUT_S, follow_redirects=False,
                        headers={"User-Agent": "VibeMind-Learning-Quellenpruefung/1"})


def fetch(url: str) -> str:
    with _client() as client:
        for _ in range(_MAX_REDIRECTS + 1):
            target = _checked(url)
            with client.stream("GET", target) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("redirect without location")
                    url = urljoin(target, location)
                    continue
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > _MAX_BYTES:
                    raise ValueError("page is too large")
                received = bytearray()
                for chunk in response.iter_bytes():
                    received += chunk
                    if len(received) > _MAX_BYTES:
                        raise ValueError("page is too large")
                return bytes(received).decode(response.encoding or "utf-8", errors="replace")
        raise ValueError("too many redirects")


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
