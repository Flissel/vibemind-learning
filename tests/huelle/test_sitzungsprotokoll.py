"""Das Sitzungsprotokoll der Huelle landet in ihrer Fehlerausgabe (huelle.err.log), sonst nichts Neues."""

from __future__ import annotations

import io
import logging

import pytest

from spaces.learning.huelle import server

NAME = "spaces.learning.huelle.sitzungen"


@pytest.fixture(autouse=True)
def _logger_zuruecksetzen() -> None:
    logger = logging.getLogger(NAME)
    vorher = (logger.level, list(logger.handlers), logger.propagate)
    logger.handlers[:] = []
    yield
    logger.setLevel(vorher[0])
    logger.handlers[:] = vorher[1]
    logger.propagate = vorher[2]


def _mitlesen() -> io.StringIO:
    """Die Ausgabe des einen Handlers in einen Puffer lenken (er schreibt sonst in die Fehlerausgabe)."""
    (ausgabe,) = logging.getLogger(NAME).handlers
    assert isinstance(ausgabe, logging.StreamHandler)
    puffer = io.StringIO()
    ausgabe.setStream(puffer)
    return puffer


def test_sitzungsprotokoll_schreibt_info_mit_uhrzeit() -> None:
    server.sitzungsprotokoll_an()
    puffer = _mitlesen()
    logging.getLogger(NAME).info("Aufgabe %d: Sitzung gebunden", 4)
    zeile = puffer.getvalue().strip()
    assert zeile.endswith("Aufgabe 4: Sitzung gebunden")
    assert zeile[4] == "-" and zeile[13] == ":"  # Datum und Uhrzeit vorn


def test_sitzungsprotokoll_zweimal_an_schreibt_einmal() -> None:
    server.sitzungsprotokoll_an()
    server.sitzungsprotokoll_an()
    puffer = _mitlesen()
    logging.getLogger(NAME).info("einmal")
    assert puffer.getvalue().count("einmal") == 1


def test_sitzungsprotokoll_laesst_die_anderen_logger_auf_warnung() -> None:
    server.sitzungsprotokoll_an()
    assert not logging.getLogger("spaces.learning.huelle.bruecke").isEnabledFor(logging.INFO)
