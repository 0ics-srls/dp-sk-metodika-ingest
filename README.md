# dp-sk-metodika-ingest

Ingest class of the `sk-metodika` package (open-lex-it phase 81b). Proc-protocol 1.1 (stdin NDJSON), Python 3.12, psycopg 3.

Reads `<dirIn>/<safe itemId>.plan.json` (written by `dp-sk-metodika-convert`, schema `plan.schema.json`, `planVersion` 1) and writes it into the
dataset schema in ONE transaction per item:

- `documents` upserted `ON CONFLICT (fonte, source_id)`; `ingested_at = now()`; the generated `search_vector` is never written.
- `passages`: when `text_raw` is unchanged and the document still has passages, passages and embeddings are kept; otherwise the passages of that
  document are deleted and the new chunks (target 3000, min 500, max 8000 characters, sentence boundaries, Slovak abbreviations `JUDr.`, `č.`, `Z. z.`
  do not split) inserted with a NULL embedding. `text_contextual = "[<ente> <tipo> <numero>]\n" + text`. (A metadata-only change keeps the old
  `text_contextual` and embedding until the text changes.)
- `skipped` replaced by `(fonte, item_ref)`; a document that is now ingested clears its stale `skipped` row.
- one `coverage` row per item replaced by `(fonte, item_id)`.
- `dryRun: true` does all writes and rolls the transaction back; the metrics are those of a real run.

Metrics: `written.<schema>.documents|passages|skipped|coverage`, `items`, `skipped_documents`, `warnings`, `passages_kept`. A failing item rolls back
alone, the others commit, and the run ends with one `system_error` listing the failed items.

## Develop

```
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest -m "not integration" tests
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m mypy .
.venv/Scripts/python -m pytest -m integration tests     # Docker: Testcontainers starts pgvector/pgvector:pg17, applies the package migrations
```

The integration tests find the package migrations (`dp-sk-metodika/schema/migrations`, 0001..0003) next to this repo through the lazy
`migrations_dir()` and create the `unaccent` extension themselves (the Host does it in production). `plan.schema.json` is the same file as in
`dp-sk-metodika-convert` (a test compares the two when both repos are checked out side by side).

## Test image

`docker build -f Dockerfile.test .` (ProcMon builds this and treats a failing run as a failed build); it runs `pytest -m "not integration"` as the
entrypoint. `tests/test_collectable.py` guards that every test module imports from a copy in a temp dir and that no test does import-time
`parents[n]` path arithmetic.

Measured 2026-11-12 (Docker Desktop, Windows): image 379 MB on disk (91 MB compressed), build 0.30 min with `--no-cache` (pip layer included), the
container runs 84 passed, 1 skipped (the sibling-schema comparison), 10 integration tests deselected.
