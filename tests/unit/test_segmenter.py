"""Sentence-boundary segmenter: golden cases ported from the legacy TextSegmenterTests plus the Slovak abbreviation cases."""
from __future__ import annotations

import pytest

from dp_sk_metodika_ingest.segmenter import MAX_CHARS, MIN_CHARS, TARGET_CHARS, segment


def test_constants() -> None:
    assert (TARGET_CHARS, MIN_CHARS, MAX_CHARS) == (3000, 500, 8000)


def test_sentence_boundaries_concatenation_is_exactly_the_input() -> None:
    text = "".join(f"Veta cislo {i} dokumentu nesie skusobny obsah. " for i in range(1, 51))

    pieces = segment(text)

    assert pieces
    assert "".join(pieces) == text


def test_twenty_thousand_chars_no_piece_over_max() -> None:
    sentence = "Danovy subjekt postupuje podla metodickeho pokynu uvedeneho v prilohe. "
    text = sentence * 300
    assert len(text) > 20000

    pieces = segment(text)

    assert all(len(p) <= MAX_CHARS for p in pieces)
    assert "".join(pieces) == text


def test_short_tail_is_merged_with_the_previous_piece() -> None:
    big = "a" * 2900 + ". "
    stub = "b" * 300 + "."

    pieces = segment(big + stub)

    assert pieces
    if len(pieces) > 1:
        assert all(len(p) >= MIN_CHARS for p in pieces)
    assert pieces[-1].endswith(stub)
    assert "".join(pieces) == big + stub


def test_oversize_text_splits_at_sentence_boundaries_into_target_sized_pieces() -> None:
    sentence = "x" * 99 + ". "
    text = sentence * 100  # 10,000 characters

    pieces = segment(text)

    assert len(pieces) >= 3
    assert all(len(p) <= MAX_CHARS for p in pieces)
    assert all(p.endswith(". ") for p in pieces)  # every cut is on a sentence boundary
    assert "".join(pieces) == text


def test_no_sentence_boundary_single_piece() -> None:
    text = "x" * 4000

    assert segment(text) == [text]


def test_no_boundary_over_max_is_hard_split() -> None:
    text = "y" * 9000

    pieces = segment(text)

    assert all(len(p) <= MAX_CHARS for p in pieces)
    assert "".join(pieces) == text


def test_empty_returns_empty_list() -> None:
    assert segment("") == []


# 2,982 characters without a dot, then a sentence that holds the abbreviation: a cut right after the abbreviation would be the first cut of a naive segmenter.
_FILLER = "Slovo " * 497


@pytest.mark.parametrize(
    "abbreviation",
    ["JUDr.", "Ing.", "Mgr.", "č.", "čl.", "ods.", "písm.", "Z. z.", "z. z.", "tzv.", "napr.", "resp.", "s. r. o.", "a. s."],
)
def test_slovak_abbreviation_does_not_split_a_sentence(abbreviation: str) -> None:
    text = f"{_FILLER}Podľa {abbreviation} Nováka a ďalších ustanovení sa postupuje podľa zákona č. 213/2001 Z. z. o pomoci. " + "Ďalšia veta nasleduje tu. " * 40

    pieces = segment(text)

    assert len(pieces) >= 2
    assert pieces[0].endswith("o pomoci. "), pieces[0][-40:]
    assert not any(p.endswith((f"{abbreviation} ", "č. ", "Z. z. ")) for p in pieces[:-1])
    assert "".join(pieces) == text


def test_a_real_sentence_end_after_a_word_still_splits() -> None:
    text = _FILLER + "Toto je koniec vety. Nova veta zacina tu a pokracuje dalej. " + "Ďalšia veta nasleduje tu. " * 40

    pieces = segment(text)

    assert pieces[0].endswith("koniec vety. ")


def test_segmentation_is_idempotent() -> None:
    text = "".join(f"Podľa JUDr. Nováka a zákona č. {i}/2001 Z. z. sa postupuje takto. " for i in range(1, 200))

    pieces = segment(text)

    assert segment("".join(pieces)) == pieces
