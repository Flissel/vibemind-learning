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
# IPv6 ranges that carry an IPv4 address in their low 32 bits (NAT64, IPv4-compatible).
_IPV4_EMBEDDING = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"), ipaddress.ip_network("::/96"))


def is_public_address(address: str) -> bool:
    """Allowlist, not denylist: only globally routable unicast addresses; IPv6 forms that
    embed an IPv4 address are judged by that address."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and any(ip in network for network in _IPV4_EMBEDDING):
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            return is_public_address(str(embedded))
    return bool(ip.is_global and not ip.is_multicast)


def resolve(host: str) -> list[str]:
    """Every address a host name resolves to (IPv4 and IPv6); patched in tests."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise OSError(str(error)) from error
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _checked(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("only public http(s) pages are fetched")
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        raise ValueError("host is not public")
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = resolve(host)
        except OSError as error:
            raise ValueError("host does not resolve") from error
    # Every answer must be public: a name with one private answer is refused as a whole.
    if not addresses or not all(is_public_address(address) for address in addresses):
        raise ValueError("host is not public")
    return url


def _client() -> httpx.Client:
    # trust_env=False: no proxy from the environment may route around the address check.
    return httpx.Client(timeout=_TIMEOUT_S, follow_redirects=False, trust_env=False,
                        headers={"User-Agent": "VibeMind-Learning-Quellenpruefung/1"})


def _peer_must_be_public(response: httpx.Response) -> None:
    """The address actually connected to (DNS may answer differently between the check and
    the connect); nothing is read from a connection to a non-public peer."""
    stream = response.extensions.get("network_stream")
    peer = stream.get_extra_info("server_addr") if stream is not None else None
    if peer and not is_public_address(str(peer[0])):
        raise ValueError("connected to a non-public address")


def fetch(url: str) -> str:
    with _client() as client:
        for _ in range(_MAX_REDIRECTS + 1):
            target = _checked(url)
            with client.stream("GET", target) as response:
                _peer_must_be_public(response)
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
