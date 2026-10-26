"""Customers of the learning platform: a usergroup in LearnHouse per customer.

    python -m spaces.learning.services.kunden.cli anlegen --name "Musterkunde GmbH" --course <course_id> [--course ...]
    python -m spaces.learning.services.kunden.cli liste

Runs inside the learning-api container (LearnHouse service URL and key from its
environment). ``anlegen`` makes the courses private to the customer and prints the
invite link; whoever signs up with it joins the customer's group. Sending the link to
real people is the operator's decision.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import httpx

_SERVICE_KEY_HEADER = "X-LearnHouse-Learning-Service-Key"


def _service() -> tuple[str, dict[str, str]]:
    url = os.environ.get("LEARNHOUSE_LEARNING_SERVICE_URL", "").rstrip("/")
    key = os.environ.get("LEARNHOUSE_LEARNING_SERVICE_KEY", "")
    if not url or len(key) < 32:
        raise SystemExit("LEARNHOUSE_LEARNING_SERVICE_URL/KEY fehlen (im learning-api-Container ausfuehren)")
    return url, {_SERVICE_KEY_HEADER: key}


def web_base() -> str:
    """The LearnHouse address people open; the embed origin is exactly that."""
    explicit = os.environ.get("LEARNHOUSE_PUBLIC_URL", "").strip()
    ancestors = os.environ.get("LEARNING_EMBED_ANCESTORS", "").split()
    return (explicit or (ancestors[0] if ancestors else "http://127.0.0.1:13000")).rstrip("/")


def create(name: str, course_ids: list[str]) -> dict[str, Any]:
    url, headers = _service()
    response = httpx.post(f"{url}/customers", headers=headers, json={"name": name, "course_ids": course_ids},
                          timeout=60)
    if response.status_code != 200:
        raise SystemExit(f"LearnHouse lehnte ab ({response.status_code}): {response.text[:300]}")
    customer = response.json()
    customer["einladung"] = web_base() + customer.pop("signup_path")
    return customer


def listing() -> list[dict[str, Any]]:
    url, headers = _service()
    response = httpx.get(f"{url}/customers", headers=headers, timeout=60)
    response.raise_for_status()
    return response.json()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kunden")
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("anlegen", help="Nutzergruppe mit privaten Kursen und Einladungslink")
    new.add_argument("--name", required=True)
    new.add_argument("--course", action="append", required=True, dest="courses")
    sub.add_parser("liste")
    args = parser.parse_args(argv)
    result: Any = create(args.name, args.courses) if args.command == "anlegen" else listing()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
