"""Sentence-boundary segmentation shared by every chunker (port of the legacy TextSegmenter, with Slovak abbreviation protection).

Accumulates sentences into pieces of about ``TARGET_CHARS``, merges a tail shorter than ``MIN_CHARS`` into the previous piece and hard-cuts
anything above ``MAX_CHARS`` (the embedding input limit). Lossless: ``"".join(segment(text)) == text``.

A dot is not a sentence end after a known Slovak abbreviation (``JUDr.``, ``č.``), after one of the letters of ``Z. z.`` / ``s. r. o.`` / ``a. s.``,
or after an initial (one upper-case letter followed by an upper-case letter, ``J. Novák``). Every other dot + whitespace cuts, as in the legacy.
"""
from __future__ import annotations

import re

TARGET_CHARS = 3000
MIN_CHARS = 500
MAX_CHARS = 8000

_SENTENCE_END = re.compile(r"\.\s+")
_LAST_WORD = re.compile(r"(\w+)$")

# Lower-case, without the dot. Titles, citation words and common abbreviations of Slovak legal and administrative text.
_ABBREVIATIONS = frozenset({
    "judr", "mudr", "mvdr", "rndr", "phdr", "paeddr", "thdr", "pharmdr", "ing", "mgr", "bc", "phd", "csc", "prof", "akad", "dr",
    "č", "čl", "ods", "písm", "odst", "príl", "str", "tzv", "napr", "resp", "príp", "atď", "atp", "tzn", "vr", "tj", "pozn", "zák", "vyhl",
    "nar", "sv", "mld", "tis", "cca", "zb",
})
# The letters of "Z. z.", "s. r. o.", "a. s.": single letters that are abbreviations in Slovak text ("n." or "l." are not, the legacy cuts there).
_ABBREVIATION_LETTERS = frozenset("zsroa")


def _is_abbreviation(text: str, dot_index: int, next_index: int) -> bool:
    """True when the dot at ``dot_index`` closes an abbreviation or an initial instead of a sentence."""
    word = _LAST_WORD.search(text, max(0, dot_index - 12), dot_index)
    if word is None:
        return False
    token = word.group(1)
    if token.lower() in _ABBREVIATIONS:
        return True
    if len(token) != 1:
        return False
    if token.lower() in _ABBREVIATION_LETTERS:
        return True
    following = text[next_index : next_index + 1]
    return token.isupper() and following.isalpha() and following.isupper()


def _split_sentences(text: str) -> list[str]:
    """Cut after every "dot + whitespace" that is a sentence end; each segment keeps its own delimiter so the concatenation reproduces the input."""
    segments: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        if _is_abbreviation(text, match.start(), match.end()):
            continue
        segments.append(text[start : match.end()])
        start = match.end()
    if start < len(text):
        segments.append(text[start:])
    return segments


def segment(text: str) -> list[str]:
    pieces: list[str] = []
    current = ""
    for sentence in _split_sentences(text):
        if current and len(current) + len(sentence) > TARGET_CHARS and len(current) >= MIN_CHARS:
            pieces.append(current)
            current = ""
        current += sentence
    if current:
        if pieces and len(current) < MIN_CHARS:
            pieces[-1] += current
        else:
            pieces.append(current)

    capped: list[str] = []
    for piece in pieces:
        if len(piece) <= MAX_CHARS:
            capped.append(piece)
            continue
        capped.extend(piece[i : i + MAX_CHARS] for i in range(0, len(piece), MAX_CHARS))
    return capped
