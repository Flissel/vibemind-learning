"""Typed numbers in the player: the reading shows live, 45,000 asks, a factor-1000 miss gets a hint."""

from __future__ import annotations

from spaces.learning.contracts.lernsequenz import LernsequenzV1
from spaces.learning.tests.unit.test_lernsequenz import sequence_data


def _open_calculation(page, player_server):
    player_server.store.save(LernsequenzV1.model_validate(sequence_data()))
    page.goto(player_server.link("seq-roi"))
    page.wait_for_function("document.getElementById('count').textContent.length > 0")
    page.click("#next")                                   # f1 -> question q1
    page.get_by_role("button", name="Überspringen").click()
    page.click("#next")                                   # f2 -> calculation roi
    page.wait_for_selector(".step input[type=text]")
    return page.locator(".step input[type=text]")


def test_typed_numbers_show_their_reading_and_ask_when_ambiguous(player_server, browser_page) -> None:
    page = browser_page
    inputs = _open_calculation(page, player_server)
    inputs.nth(0).fill("1288")
    page.wait_for_function("document.querySelectorAll('.reading')[0].textContent.includes('1.288')")
    assert page.locator(".reading").nth(0).inner_text() == "= 1.288 h"

    inputs.nth(1).fill("45,000")
    page.wait_for_selector(".reading button")
    assert "Meinen Sie" in page.locator(".reading").nth(1).inner_text()
    page.locator(".reading").nth(1).get_by_role("button", name="45.000 €").click()
    assert inputs.nth(1).input_value() == "45000"
    assert page.errors == []


def test_a_factor_1000_miss_gets_the_separator_hint(player_server, browser_page) -> None:
    page = browser_page
    inputs = _open_calculation(page, player_server)
    inputs.nth(0).fill("1000")
    inputs.nth(1).fill("45,000")
    page.locator(".step button.primary").first.click()
    page.wait_for_function("document.querySelectorAll('.step .small:not(.hidden)').length >= 2")
    notes = page.locator(".step").nth(1).inner_text()
    assert "Tausender-Trennzeichen" in notes
    assert page.errors == []
