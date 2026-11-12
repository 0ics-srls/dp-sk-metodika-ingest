"""Passages of a guidance document (port of the branch-53 chunker).

No header is known, so every piece is a body piece: the sentence-boundary segmenter over ``text_raw`` with the contextual prefix
``[<ente> <tipo> <numero>]`` in front of the embedded text (the Slovak numero is a code, so there is no ``n. x/y`` form).
Pure, deterministic, lossless: ``"".join(p.text) == text_raw``.
"""
from __future__ import annotations

from dataclasses import dataclass

from dp_sk_metodika_ingest.segmenter import segment


@dataclass(frozen=True)
class Piece:
    """One embeddable passage: ``text`` is stored and shown, ``text_contextual`` (prefix + text) is what gets embedded."""

    seq: int
    kind: str
    text: str
    text_contextual: str


def _prefix(ente: str, tipo: str, numero: str | None) -> str:
    out = f"[{ente} {tipo}"
    if numero is not None and numero.strip():
        out += f" {numero}"
    return out + "]\n"


def chunk_document(text_raw: str, ente: str, tipo: str, numero: str | None) -> list[Piece]:
    """The passages of one document, ``seq`` from 0; no passages for a blank text."""
    if not text_raw or not text_raw.strip():
        return []
    prefix = _prefix(ente, tipo, numero)
    return [Piece(seq, "body", text, prefix + text) for seq, text in enumerate(segment(text_raw))]
