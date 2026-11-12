"""Chunker: contextual prefix of the Slovak estremi, every piece a body piece, lossless, deterministic; the cut points equal the legacy chunker (golden)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dp_sk_metodika_ingest.chunker import Piece, chunk_document
from dp_sk_metodika_ingest.segmenter import MAX_CHARS

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
ENTE = "Finančná správa SR"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    ("tipo", "numero", "prefix"),
    [
        ("MP", "10/DZPaU/2026/MP", "[Finančná správa SR MP 10/DZPaU/2026/MP]\n"),
        ("dokument", None, "[Finančná správa SR dokument]\n"),
        ("MU", "   ", "[Finančná správa SR MU]\n"),
    ],
)
def test_prefix_variants(tipo: str, numero: str | None, prefix: str) -> None:
    pieces = chunk_document("Text metodického pokynu.", ENTE, tipo, numero)

    assert [p.text_contextual for p in pieces] == [prefix + "Text metodického pokynu."]
    assert "  " not in pieces[0].text_contextual


def test_every_piece_is_a_body_piece_and_nothing_is_lost() -> None:
    text = "".join(f"Odsek {i} metodického pokynu rieši osobitný daňový profil. " for i in range(1, 80))

    pieces = chunk_document(text, ENTE, "MP", "1/2026")

    assert pieces
    assert {p.kind for p in pieces} == {"body"}
    assert "".join(p.text for p in pieces) == text
    assert [p.seq for p in pieces] == list(range(len(pieces)))


def test_chunks_are_deterministic() -> None:
    text = "Skúšobná veta dostatočne dlhá na naplnenie kusa. " * 200

    assert chunk_document(text, ENTE, "MU", "9") == chunk_document(text, ENTE, "MU", "9")


@pytest.mark.parametrize("empty", ["", "   ", "\n\n"])
def test_empty_text_gives_no_chunks(empty: str) -> None:
    assert chunk_document(empty, ENTE, "MP", "1") == []


def test_a_text_shorter_than_the_minimum_is_one_passage() -> None:
    pieces = chunk_document("Krátky text.", ENTE, "MP", "1")

    assert len(pieces) == 1
    assert pieces[0].seq == 0


def test_a_2500_char_text_is_one_passage() -> None:
    text = ("Vety sa opakujú a držia sa pokope. " * 100)[:2500]

    assert len(text) == 2500
    assert len(chunk_document(text, ENTE, "MP", "1")) == 1


def test_a_35000_char_text_gives_8_to_14_passages() -> None:
    text = ("Toto je veta metodického pokynu o dani z pridanej hodnoty. " * 700)[:35_000]

    pieces = chunk_document(text, ENTE, "MP", "1")

    assert 8 <= len(pieces) <= 14
    assert all(len(p.text) <= MAX_CHARS for p in pieces)


def test_every_piece_within_max_chars() -> None:
    pieces = chunk_document("x" * 30_000, ENTE, "MP", "1")

    assert len(pieces) == 4
    assert all(len(p.text) <= MAX_CHARS for p in pieces)


def test_piece_is_a_value_object() -> None:
    assert Piece(0, "body", "a", "[x]\na") == Piece(0, "body", "a", "[x]\na")


def test_should_cut_the_real_text_exactly_like_the_legacy_chunker() -> None:
    golden = json.loads((FIXTURES / "chunk-golden.json").read_text(encoding="utf-8"))
    text = (FIXTURES / "chunk-input.txt").read_bytes().decode("utf-8")
    assert _sha(text) == golden["source_text_sha256"]
    assert len(text) == golden["source_text_len"]

    pieces = chunk_document(text, ENTE, "MP", "10")

    assert len(pieces) == len(golden["pieces"]) == 11
    for piece, expected in zip(pieces, golden["pieces"], strict=True):
        assert (piece.seq, piece.kind, len(piece.text)) == (expected["seq"], expected["kind"], expected["text_len"])
        assert _sha(piece.text) == expected["text_sha256"]
        assert piece.text_contextual == f"[{ENTE} MP 10]\n" + piece.text
