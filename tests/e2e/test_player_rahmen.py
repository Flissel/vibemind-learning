"""Mark a place on a slide: have it explained from the course, or keep it as a note."""

from __future__ import annotations

import httpx

from spaces.learning.contracts.lernsequenz import LernsequenzV1
from spaces.learning.services.lernsequenz.person import person_token
from spaces.learning.tests.e2e.conftest import PLAYER_KEY
from spaces.learning.tests.unit.test_lernsequenz import sequence_data


def _drag(page, box: dict, x0: float, y0: float, x1: float, y1: float) -> None:
    page.mouse.move(box["x"] + x0, box["y"] + y0)
    page.mouse.down()
    page.mouse.move(box["x"] + x1, box["y"] + y1, steps=6)
    page.mouse.up()


def test_marked_place_is_explained_and_kept_as_a_note(player_server, explain_worker, browser_page) -> None:
    page = browser_page
    player_server.store.save(LernsequenzV1.model_validate(sequence_data()))
    page.goto(player_server.link("seq-roi") + "&p=" + person_token("lh-3-lernende", key=PLAYER_KEY))
    page.wait_for_function("document.getElementById('count').textContent.length > 0")
    title = page.locator("#stage h2").inner_text()

    page.get_by_role("button", name="Stelle markieren").click()
    stage = page.locator("#stage").bounding_box()
    _drag(page, stage, 20, 20, stage["width"] - 40, stage["height"] - 40)          # across the title
    page.wait_for_selector(".markpop")
    page.get_by_role("button", name="Erklär mir das").click()
    page.wait_for_selector(".markpop .answer")
    assert "Die Jahreskosten laut Folie." in page.locator(".markpop .answer").inner_text()
    assert "Nicht aus dem Kurs" in page.locator(".markpop").inner_text()
    assert title in explain_worker.calls[0]["task"]["selection"]

    page.get_by_role("button", name="Notiz anlegen").click()
    page.wait_for_selector(".markpop .saved")
    notes = httpx.get(player_server.base + "/api/v1/notizen",
                      headers={"X-LearnHouse-Learning-Service-Key": PLAYER_KEY, "X-Learning-Actor": "lh-3-lernende"}).json()["notes"]
    assert len(notes) == 1 and title in notes[0]["selection"] and notes[0]["anchor"] == {"section_id": "f1"}
    assert notes[0]["explanation"] == "Die Jahreskosten laut Folie." and notes[0]["extra"].startswith("Allgemein")
    assert page.errors == []


def test_marking_without_a_person_still_explains_but_cannot_keep_a_note(player_server, explain_worker, browser_page) -> None:
    page = browser_page
    player_server.store.save(LernsequenzV1.model_validate(sequence_data()))
    page.goto(player_server.link("seq-roi"))
    page.wait_for_function("document.getElementById('count').textContent.length > 0")
    page.get_by_role("button", name="Stelle markieren").click()
    stage = page.locator("#stage").bounding_box()
    _drag(page, stage, 20, 20, stage["width"] - 40, stage["height"] - 40)
    page.wait_for_selector(".markpop")
    assert page.get_by_role("button", name="Notiz anlegen").is_disabled()
    assert "anmelden" in page.locator(".markpop").inner_text().lower()


def test_without_a_tutor_the_player_says_so_in_german(player_server, browser_page, monkeypatch) -> None:
    monkeypatch.delenv("LEARNING_EVALUATION_SERVICE_URL", raising=False)
    page = browser_page
    player_server.store.save(LernsequenzV1.model_validate(sequence_data()))
    page.goto(player_server.link("seq-roi"))
    page.wait_for_function("document.getElementById('count').textContent.length > 0")
    page.get_by_role("button", name="Stelle markieren").click()
    stage = page.locator("#stage").bounding_box()
    _drag(page, stage, 20, 20, stage["width"] - 40, stage["height"] - 40)
    page.wait_for_selector(".markpop")
    page.get_by_role("button", name="Erklär mir das").click()
    page.wait_for_function("document.querySelector('.markpop .small[aria-live]').textContent.length > 0")
    assert "Tutor ist gerade nicht erreichbar" in page.locator(".markpop .small[aria-live]").inner_text()
