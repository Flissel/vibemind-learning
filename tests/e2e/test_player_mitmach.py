"""The hands-on checkpoints in a real browser: profile filters, ordering, cloze, matching,
hotspot, terminal sim and step all render and grade."""

from __future__ import annotations

import base64
from pathlib import Path

from spaces.learning.contracts.lernsequenz import LernsequenzV1
from spaces.learning.tests.unit.test_lernsequenz_mitmach import mitmach_data

# A 2x2 transparent PNG; the player scales it to the checkpoint's width/height.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAC0lEQVR42mNkAAIAAAoAAv/lxKUAAAAASUVORK5CYII="
)
ORDERED = ["Konto anlegen", "Installer herunterladen", "Anmelden"]


def _sequence(player_server, tmp_path: Path, *, with_three_shells: bool = False) -> str:
    data = mitmach_data()
    data["sequence_id"] = "seq-mitmach"
    if with_three_shells:
        data["checkpoints"][6]["commands"] = [
            {"shell": "powershell", "command": "claude --version"},
            {"shell": "cmd", "command": "claude --version"},
            {"shell": "unix", "command": "claude --version"},
        ]
    # The same blank twice: typed once, mirrored into the second place.
    data["checkpoints"][2]["template"] = "irm https://claude.ai/{{datei}} | iex ; echo {{datei}}"
    media = tmp_path / "lernsequenzen" / "medien"
    media.mkdir(parents=True, exist_ok=True)
    (media / "app.png").write_bytes(_PNG)
    player_server.store.save(LernsequenzV1.model_validate(data))
    return player_server.link("seq-mitmach")


def _open(page, link: str) -> None:
    page.goto(link)
    page.wait_for_function("document.getElementById('count').textContent.length > 0")
    page.evaluate("localStorage.clear()")


def _answer_profile(page, shell_label: str) -> None:
    page.click("#next")
    page.wait_for_selector(".overlay input[type=radio]")
    page.get_by_label(shell_label).check()
    page.locator(".overlay footer .btn.primary").click()


def _skip(page) -> None:
    page.get_by_role("button", name="Überspringen").click()


def _weiter(page) -> None:
    # The page's own "Weiter" (#next) sits behind the overlay; only the frame's button counts.
    page.locator(".overlay").get_by_role("button", name="Weiter").click()


def test_profile_filters_slides_and_preselects_shell(player_server, browser_page, tmp_path) -> None:
    page = browser_page
    _open(page, _sequence(player_server, tmp_path, with_three_shells=True))
    _answer_profile(page, "Windows CMD")
    # Slide 2 is for PowerShell only: with CMD chosen its checkpoints (no condition of their
    # own) open here, after slide 1's three, and the sequence ends without slide 2.
    for _ in range(6):
        page.wait_for_selector(".overlay")
        _skip(page)
    page.wait_for_function("document.querySelector('#stage h2') && document.querySelector('#stage h2').textContent === 'Geschafft'")
    assert page.evaluate("JSON.parse(localStorage.getItem('lernsequenz-profil')).shell") == "cmd"
    assert page.errors == []

    _open(page, player_server.link("seq-mitmach"))
    _answer_profile(page, "Windows PowerShell")
    for _ in range(3):
        page.wait_for_selector(".overlay")
        _skip(page)
    page.wait_for_function("document.getElementById('count').textContent.startsWith('2 /')")
    page.click("#next")                                # hotspot, terminal, step after slide 2
    page.wait_for_selector(".overlay")
    _skip(page)
    page.wait_for_selector(".overlay")
    _skip(page)
    page.wait_for_selector(".overlay .tab.sel")
    assert page.locator(".overlay .tab.sel").inner_text() == "Windows PowerShell"
    assert page.errors == []


def test_ordering_can_be_solved_with_buttons(player_server, browser_page, tmp_path) -> None:
    page = browser_page
    _open(page, _sequence(player_server, tmp_path))
    _answer_profile(page, "Windows PowerShell")
    page.wait_for_selector(".overlay .order")
    rows = page.locator(".overlay .order .row-text")
    for target_index, text in enumerate(ORDERED):
        while rows.nth(target_index).inner_text() != text:
            current = [rows.nth(i).inner_text() for i in range(rows.count())].index(text)
            page.locator(".overlay .order button[aria-label='Nach oben']").nth(current).click()
    page.get_by_role("button", name="Prüfen").click()
    page.wait_for_selector(".overlay .fb.ok")
    assert "Genau" in page.locator(".overlay .fb").inner_text()
    assert page.errors == []


def test_cloze_matching_hotspot_terminal_and_step_grade(player_server, browser_page, tmp_path) -> None:
    page = browser_page
    _open(page, _sequence(player_server, tmp_path))
    _answer_profile(page, "Windows PowerShell")
    page.wait_for_selector(".overlay .order")
    _skip(page)

    page.wait_for_selector(".overlay input.blank")
    page.fill(".overlay input.blank", "install.ps1")
    assert page.locator(".overlay .blank.mirror").inner_text() == "install.ps1"
    page.get_by_role("button", name="Prüfen").click()
    page.wait_for_selector(".overlay .fb.ok")
    _weiter(page)

    page.wait_for_selector(".overlay select")
    labels = {"irm … | iex": "PowerShell", "curl … install.cmd": "CMD", "curl … | bash": "macOS / Linux"}
    for row in page.locator(".overlay .match").all():
        row.locator("select").select_option(label=labels[row.locator(".row-text").inner_text()])
    page.get_by_role("button", name="Prüfen").click()
    page.wait_for_selector(".overlay .fb.ok")
    _weiter(page)

    page.wait_for_function("document.getElementById('count').textContent.startsWith('2 /')")
    page.click("#next")
    page.wait_for_selector(".overlay .hotspot img")
    box = page.locator(".overlay .hotspot img").bounding_box()
    scale_x, scale_y = box["width"] / 1200, box["height"] / 800
    page.mouse.move(box["x"] + 290 * scale_x, box["y"] + 30 * scale_y)
    page.mouse.down()
    page.mouse.move(box["x"] + 390 * scale_x, box["y"] + 80 * scale_y, steps=5)
    page.mouse.up()
    page.get_by_role("button", name="Prüfen").click()
    page.wait_for_selector(".overlay .fb.ok")
    assert "Getroffen" in page.locator(".overlay .fb").inner_text()
    _weiter(page)

    page.wait_for_selector(".overlay .terminal input")
    page.fill(".overlay .terminal input", "claude --version")
    page.keyboard.press("Enter")
    page.wait_for_selector(".overlay .fb.ok")
    assert "2.1.211 (Claude Code)" in page.locator(".overlay .terminal").inner_text()
    _weiter(page)

    page.wait_for_selector(".overlay")
    page.get_by_role("button", name="Sieht bei mir so aus").click()
    page.wait_for_function("document.querySelector('#stage h2') && document.querySelector('#stage h2').textContent === 'Geschafft'")
    assert page.errors == []


def test_person_token_is_forwarded_with_every_answer(player_server, browser_page, tmp_path) -> None:
    page = browser_page
    link = _sequence(player_server, tmp_path) + "&p=abc.def"
    page.goto(link)
    page.wait_for_function("document.getElementById('count').textContent.length > 0")
    page.evaluate("localStorage.clear()")
    with page.expect_request(lambda r: "/antwort" in r.url) as answer:
        _answer_profile(page, "Windows PowerShell")
    assert "p=abc.def" in answer.value.url
    page.wait_for_selector(".overlay .fb.no")
    assert "neu laden" in page.locator(".overlay .fb.no").inner_text()
