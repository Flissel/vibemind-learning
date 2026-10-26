"""Laura explainer videos: narration pieces fit one still, slide times come from Laura's beats."""

from __future__ import annotations

from spaces.learning.services.lernsequenz.laura_video import FPS, pieces, slide_times


def test_pieces_fit_one_still_and_keep_every_word() -> None:
    text = (
        "Ein Akku-Bohrschrauber ist ein Werkzeug fuer Bohren und Schrauben. "
        "Vor jedem Einsatz pruefst du Akku, Gehaeuse und Bohrfutter, denn ein beschaedigtes Geraet, "
        "das trotzdem benutzt wird, kann ueberhitzen, ploetzlich blockieren oder dich verletzen. Kurz."
    )
    result = pieces(text)
    assert all(len(p) <= 110 for p in result)
    assert " ".join(result).split() == text.split()
    assert len(result) >= 3


def test_one_long_word_run_is_still_split() -> None:
    assert all(len(p) <= 110 for p in pieces("wort " * 80))


def test_slide_times_span_all_beats_of_a_slide() -> None:
    beats = [{"seq_in": 0, "seq_out": 270}, {"seq_in": 270, "seq_out": 480}, {"seq_in": 480, "seq_out": 750}]
    times = slide_times([0, 0, 1], beats, ["s1", "s2"])
    assert times == {"s1": {"start": 0.0, "end": round(480 / FPS, 2)},
                     "s2": {"start": round(480 / FPS, 2), "end": round(750 / FPS, 2)}}
