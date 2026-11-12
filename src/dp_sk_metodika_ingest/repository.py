"""SQL of one item (plan file v1): documents, passages, skipped items and coverage.

All statements run inside one transaction opened by the caller; the dataset schema is on the search_path (names are unqualified). The rules:

* a document is upserted on ``(fonte, source_id)``; its ``id`` survives, ``ingested_at`` is set to ``now()`` and the generated ``search_vector`` is never
  written;
* when ``text_raw`` is unchanged and the document still has passages, its passages and embeddings are kept untouched; otherwise the passages of that
  document are deleted and the new chunks inserted with a NULL embedding (the embed step fills it);
* ``skipped`` rows are replaced by ``(fonte, item_ref)``; a document that is now ingested clears its stale ``skipped`` row;
* one ``coverage`` row per item is replaced by ``(fonte, item_id)``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from dp_sk_metodika_ingest.chunker import chunk_document

if TYPE_CHECKING:
    from collections.abc import Iterable

PLAN_SCHEMA_VERSION = 1
TABLES = ("documents", "passages", "skipped", "coverage")

DOCUMENT_COLUMNS = (
    "fonte", "source_id", "legacy_doc_id", "ente", "tipo", "section", "group_label", "numero", "number", "dept", "anno", "data", "oggetto",
    "text_raw", "text_source", "source_url", "pdf_bytes", "pdf_pages", "pdf_created",
)
_UPDATED = tuple(c for c in DOCUMENT_COLUMNS if c not in ("fonte", "source_id"))
_SET_ALL = ", ".join(f"{c} = EXCLUDED.{c}" for c in _UPDATED)

FIND_DOCUMENT = """
SELECT d.id, d.text_raw = %s, (SELECT count(*) FROM passages p WHERE p.document_id = d.id)
FROM documents d WHERE d.fonte = %s AND d.source_id = %s
"""
UPSERT_DOCUMENT = f"""
INSERT INTO documents ({", ".join(DOCUMENT_COLUMNS)}, ingested_at)
VALUES ({", ".join(["%s"] * len(DOCUMENT_COLUMNS))}, now())
ON CONFLICT (fonte, source_id) DO UPDATE SET {_SET_ALL}, ingested_at = now()
RETURNING id
"""
DELETE_PASSAGES = "DELETE FROM passages WHERE document_id = %s"
INSERT_PASSAGE = "INSERT INTO passages (document_id, seq, text, text_contextual) VALUES (%s, %s, %s, %s)"

CLEAR_SKIPPED = "DELETE FROM skipped WHERE fonte = %s AND item_ref = ANY(%s)"
UPSERT_SKIPPED = """
INSERT INTO skipped (fonte, item_ref, reason) VALUES (%s, %s, %s)
ON CONFLICT (fonte, item_ref) DO UPDATE SET reason = EXCLUDED.reason, seen_at = now()
"""
UPSERT_COVERAGE = """
INSERT INTO coverage (fonte, item_id, listed, taken, skipped, note) VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (fonte, item_id) DO UPDATE SET
    listed = EXCLUDED.listed, taken = EXCLUDED.taken, skipped = EXCLUDED.skipped, note = EXCLUDED.note, noted_at = now()
"""


class Cursor(Protocol):
    def execute(self, sql: Any, params: Any = None) -> Any: ...

    def executemany(self, sql: Any, params: Iterable[Any]) -> Any: ...


def write_document(cursor: Cursor, doc: dict[str, Any]) -> dict[str, int]:
    """Writes one document and, unless its text is unchanged, its passages; returns ``documents``, ``passages`` (inserted) and ``passages_kept``."""
    if not isinstance(doc["text_raw"], str) or not doc["text_raw"].strip():
        msg = f"{doc['source_id']}: empty text, the document is not written"
        raise ValueError(msg)
    existing = cursor.execute(FIND_DOCUMENT, (doc["text_raw"], doc["fonte"], doc["source_id"])).fetchone()
    keep = existing is not None and bool(existing[1]) and int(existing[2]) > 0
    document_id = cursor.execute(UPSERT_DOCUMENT, tuple(doc[c] for c in DOCUMENT_COLUMNS)).fetchone()[0]
    if keep:
        return {"documents": 1, "passages": 0, "passages_kept": int(existing[2])}
    pieces = chunk_document(doc["text_raw"], doc["ente"], doc["tipo"], doc["numero"])
    cursor.execute(DELETE_PASSAGES, (document_id,))
    cursor.executemany(INSERT_PASSAGE, [(document_id, p.seq, p.text, p.text_contextual) for p in pieces])
    return {"documents": 1, "passages": len(pieces), "passages_kept": 0}


def clear_skipped(cursor: Cursor, fonte: str, source_ids: list[str]) -> None:
    cursor.execute(CLEAR_SKIPPED, (fonte, source_ids))


def write_skipped(cursor: Cursor, rows: list[tuple[str, str, str]]) -> int:
    if rows:
        cursor.executemany(UPSERT_SKIPPED, rows)
    return len(rows)


def write_coverage(cursor: Cursor, coverage: dict[str, Any]) -> int:
    cursor.execute(UPSERT_COVERAGE, (coverage["fonte"], coverage["item_id"], coverage["listed"], coverage["taken"], coverage["skipped"], coverage["note"]))
    return 1
