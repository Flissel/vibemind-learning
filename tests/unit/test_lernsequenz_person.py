"""The person behind the player: a signed, expiring token LearnHouse hands to the embed."""

from __future__ import annotations

import pytest

from spaces.learning.services.lernsequenz.person import TTL_SECONDS, person_token, read_person_token

KEY = "shared-with-learnhouse-" + "x" * 16


def test_token_round_trip() -> None:
    token = person_token("lh-3-anna", key=KEY, now=1_000.0)
    assert read_person_token(token, key=KEY, now=1_000.0) == "lh-3-anna"
    assert token.count(".") == 1 and "lh-3-anna" not in token.split(".")[1]


def test_token_expires_after_a_day() -> None:
    token = person_token("lh-3-anna", key=KEY, now=1_000.0)
    assert read_person_token(token, key=KEY, now=1_000.0 + TTL_SECONDS) == "lh-3-anna"
    with pytest.raises(LookupError, match="expired"):
        read_person_token(token, key=KEY, now=1_000.0 + TTL_SECONDS + 1)


def test_tampered_or_foreign_tokens_are_invalid() -> None:
    token = person_token("lh-3-anna", key=KEY, now=1_000.0)
    payload, signature = token.split(".")
    with pytest.raises(LookupError, match="invalid"):
        read_person_token(payload + "." + signature[:-2] + "zz", key=KEY, now=1_000.0)
    with pytest.raises(LookupError, match="invalid"):
        read_person_token(token, key="another-key-of-enough-length-xxxxxx", now=1_000.0)
    with pytest.raises(LookupError, match="invalid"):
        read_person_token("", key=KEY, now=1_000.0)
