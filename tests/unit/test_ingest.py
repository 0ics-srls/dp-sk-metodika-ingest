"""run_ingest with a fake connection: statement order, parameters, transactions, the embedding rule, skipped and coverage, dryRun, metrics, secrets."""
from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from builders import ITEM_ID, LONG_TEXT, FakeConnection, coverage, document, item, new_plan, write_plan
from dp_sk_metodika_ingest.errors import CONFIG_ERROR, SYSTEM_ERROR, TRANSIENT_ERROR, IngestError
from dp_sk_metodika_ingest.ingest import run_ingest, safe_name

CONN = "Host=db;Port=5432;Database=lex;Username=ds_sk_metodika_rw;Password=s3cr3t-pw"
SCHEMA = "ds_sk_metodika"
OTHER_ID = "pdf:nbs-metodicke:all"


def params(directory: Path, *items: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"items": list(items), "dirIn": str(directory), "connString": CONN, "schema": SCHEMA, **extra}


def run(directory: Path, connection: FakeConnection, *items: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return run_ingest(params(directory, *items, **extra), lambda _dsn: connection, lambda *_: None)


def kinds(connection: FakeConnection) -> list[str]:
    return [" ".join(sql.split()[:3]) for _, sql, _ in connection.log]


def statement(connection: FakeConnection, text: str) -> tuple[str, str, Any]:
    return next(entry for entry in connection.log if text in entry[1])


def test_the_plan_file_name_is_the_safe_item_id() -> None:
    assert safe_name("fs:metodicke-pokyny") == "fs_metodicke-pokyny"
    assert safe_name("pdf:mfsr-spravodajca:2026") == "pdf_mfsr-spravodajca_2026"


def test_a_new_document_runs_the_statements_in_order_inside_one_transaction(tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document()], [{"fonte": "financnasprava", "item_ref": "x", "reason": "not_a_pdf"}]))
    connection = FakeConnection()

    result = run(tmp_path, connection, item())

    assert connection.events == ["begin", "commit"]
    assert kinds(connection) == [
        "SET search_path TO",
        "SELECT d.id, d.text_raw",
        "INSERT INTO documents",
        "DELETE FROM passages",
        "INSERT INTO passages",
        "DELETE FROM skipped",
        "INSERT INTO skipped",
        "INSERT INTO coverage",
    ]
    assert statement(connection, "SET search_path")[1] == f"SET search_path TO {SCHEMA}, public"
    assert connection.closed
    assert result["loaded"] == [f"{ITEM_ID}|1"]
    metrics = result["metrics"]
    assert (metrics[f"written.{SCHEMA}.documents"], metrics[f"written.{SCHEMA}.passages"]) == (1, 1)
    assert (metrics[f"written.{SCHEMA}.skipped"], metrics[f"written.{SCHEMA}.coverage"]) == (1, 1)
    assert (metrics["items"], metrics["skipped_documents"]) == (1, 1)


def test_the_plan_is_read_from_the_file_of_the_item(tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document(source_id="in-plan")]))
    connection = FakeConnection()

    run(tmp_path, connection, item())

    assert statement(connection, "INSERT INTO documents")[2][1] == "in-plan"


def test_the_document_is_upserted_on_fonte_and_source_id_without_touching_generated_columns(tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document()]))
    connection = FakeConnection()

    run(tmp_path, connection, item())

    _, sql, args = statement(connection, "INSERT INTO documents")
    assert "ON CONFLICT (fonte, source_id) DO UPDATE" in sql
    assert "RETURNING id" in sql
    assert "ingested_at = now()" in sql
    assert "search_vector" not in sql, "the generated column is never written"
    assert not re.search(r"(?<![a-z_])id = ", sql.split("DO UPDATE SET")[1]), "the id of an existing document is never replaced"
    assert args[:3] == ("financnasprava", "Zverejnovanie_dok_10_DZPaU_2026_MP", "10_DZPaU_2026_MP")
    assert args[3:5] == ("Finančná správa SR", "MP")
    assert len(args) == 19


def test_passages_are_inserted_with_a_null_embedding_and_the_contextual_prefix(tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document()]))
    connection = FakeConnection()

    run(tmp_path, connection, item())

    _, sql, rows = statement(connection, "INSERT INTO passages")
    assert "embedding" not in sql
    assert len(rows) == 1
    assert rows[0][1] == 0
    assert rows[0][3].startswith("[Finančná správa SR MP 10/DZPaU/2026/MP]\n")


def test_a_changed_text_deletes_the_passages_of_that_document_and_inserts_new_ones(tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document(text_raw=LONG_TEXT)]))
    connection = FakeConnection(answers={"FROM documents d": (uuid.UUID(int=77), False, 4)})

    run(tmp_path, connection, item())

    _, sql, args = statement(connection, "DELETE FROM passages")
    assert "WHERE document_id = %s" in sql
    assert isinstance(args[0], uuid.UUID)  # the id RETURNING gave
    _, _, rows = statement(connection, "INSERT INTO passages")
    assert len(rows) >= 3
    assert kinds(connection).index("DELETE FROM passages") < kinds(connection).index("INSERT INTO passages")


def test_an_unchanged_text_keeps_the_passages_and_their_embeddings(tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document(text_raw=LONG_TEXT)]))
    connection = FakeConnection(answers={"FROM documents d": (uuid.UUID(int=77), True, 5)})

    result = run(tmp_path, connection, item())

    assert "DELETE FROM passages" not in " ".join(kinds(connection))
    assert "INSERT INTO passages" not in " ".join(kinds(connection))
    assert "INSERT INTO documents" in " ".join(kinds(connection)), "the metadata is still upserted"
    assert result["metrics"][f"written.{SCHEMA}.passages"] == 0
    assert result["metrics"]["passages_kept"] == 5


def test_an_unchanged_text_without_passages_gets_them(tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document()]))
    connection = FakeConnection(answers={"FROM documents d": (uuid.UUID(int=77), True, 0)})

    run(tmp_path, connection, item())

    assert "INSERT INTO passages" in " ".join(kinds(connection))


def test_a_document_that_is_now_ingested_clears_its_stale_skipped_row(tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document(source_id="a"), document(source_id="b")]))
    connection = FakeConnection()

    run(tmp_path, connection, item())

    _, sql, args = statement(connection, "DELETE FROM skipped")
    assert "fonte = %s AND item_ref = ANY(%s)" in sql
    assert args == ("financnasprava", ["a", "b"])


def test_skipped_rows_are_replaced_by_fonte_and_item_ref(tmp_path: Path) -> None:
    skipped = [{"fonte": "financnasprava", "item_ref": "scan", "reason": "ocr_empty"}, {"fonte": "financnasprava", "item_ref": "big", "reason": "too_large"}]
    write_plan(tmp_path, new_plan([], skipped))
    connection = FakeConnection()

    result = run(tmp_path, connection, item())

    _, sql, rows = statement(connection, "INSERT INTO skipped")
    assert "ON CONFLICT (fonte, item_ref) DO UPDATE" in sql
    assert rows == [("financnasprava", "scan", "ocr_empty"), ("financnasprava", "big", "too_large")]
    assert result["metrics"][f"written.{SCHEMA}.skipped"] == 2
    assert "INSERT INTO documents" not in " ".join(kinds(connection))


def test_one_coverage_row_per_item_is_replaced_by_fonte_and_item_id(tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document()], cover=coverage(listed=7, taken=5, skipped=2, note="zip_archives_only;layout_c_not_collected")))
    connection = FakeConnection()

    run(tmp_path, connection, item())

    _, sql, args = statement(connection, "INSERT INTO coverage")
    assert "ON CONFLICT (fonte, item_id) DO UPDATE" in sql
    assert args == ("financnasprava", ITEM_ID, 7, 5, 2, "zip_archives_only;layout_c_not_collected")


def test_an_empty_item_still_writes_its_coverage(tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([], cover=coverage(listed=0, taken=0, skipped=0)))
    connection = FakeConnection()

    result = run(tmp_path, connection, item())

    assert kinds(connection) == ["SET search_path TO", "INSERT INTO coverage"]
    assert result["loaded"] == [f"{ITEM_ID}|1"]


def test_two_items_run_in_two_transactions_and_a_failing_one_rolls_back_alone(tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document(source_id="a")]))
    write_plan(tmp_path, new_plan([document(fonte="nbs-metodicke", source_id="b")], item_id=OTHER_ID, fonte="nbs-metodicke"))
    connection = FakeConnection(fail_on="INSERT INTO passages")

    with pytest.raises(IngestError) as caught:
        run(tmp_path, connection, item(), item(OTHER_ID, "pdf"))

    assert connection.events == ["begin", "rollback", "begin", "rollback"]
    assert caught.value.code == SYSTEM_ERROR
    assert f"{ITEM_ID}|1" in str(caught.value)
    assert f"{OTHER_ID}|1" in str(caught.value)
    assert "s3cr3t-pw" not in str(caught.value)


def test_a_failing_item_does_not_stop_the_next_one(tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document(source_id="a")]))
    # the second item has no plan file at all: it fails before its transaction opens
    connection = FakeConnection()

    with pytest.raises(IngestError) as caught:
        run(tmp_path, connection, item(OTHER_ID, "pdf"), item())

    assert connection.events == ["begin", "commit"]
    assert OTHER_ID in str(caught.value)


def test_dry_run_writes_inside_the_transaction_and_rolls_it_back(tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document()]))
    connection = FakeConnection()

    result = run(tmp_path, connection, item(), dryRun=True)

    assert connection.events == ["begin", "rollback"]
    assert "INSERT INTO documents" in " ".join(kinds(connection))
    assert result["loaded"] == [f"{ITEM_ID}|1"]
    assert result["metrics"][f"written.{SCHEMA}.documents"] == 1


def test_dry_run_null_is_a_real_run(tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document()]))
    connection = FakeConnection()

    run(tmp_path, connection, item(), dryRun=None)

    assert connection.events == ["begin", "commit"]


def test_the_plan_of_another_item_is_refused(tmp_path: Path) -> None:
    plan = new_plan([document()], item_id=OTHER_ID, fonte="nbs-metodicke")
    (tmp_path / "fs_metodicke-pokyny.plan.json").write_text(json.dumps(plan), encoding="utf-8")
    connection = FakeConnection()

    with pytest.raises(IngestError):
        run(tmp_path, connection, item())

    assert connection.events == []


def test_the_failure_message_and_the_log_never_carry_the_password(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    write_plan(tmp_path, new_plan([document()]))

    def refuse(_dsn: str) -> FakeConnection:
        raise OSError(CONN)

    with caplog.at_level(logging.DEBUG), pytest.raises(IngestError) as caught:
        run_ingest(params(tmp_path, item()), refuse, lambda *_: None)

    assert caught.value.code == TRANSIENT_ERROR
    assert "s3cr3t-pw" not in str(caught.value)
    assert "s3cr3t-pw" not in caplog.text


@pytest.mark.parametrize(
    "bad",
    [
        {"items": []},
        {"dirIn": ""},
        {"connString": ""},
        {"schema": "public"},
        {"schema": "ds_x; DROP"},
        {"items": [{"key": "k", "meta": {"kind": "ade", "itemId": "x"}}]},
        {"items": [{"key": "k", "meta": {"kind": "fs"}}]},
    ],
)
def test_bad_parameters_are_config_errors(tmp_path: Path, bad: dict[str, Any]) -> None:
    merged = {**params(tmp_path, item()), **bad}

    with pytest.raises(IngestError) as caught:
        run_ingest(merged, lambda _dsn: FakeConnection(), lambda *_: None)

    assert caught.value.code == CONFIG_ERROR


def test_a_plan_that_breaks_the_schema_fails_the_item(tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document(text_source="scan")]))
    connection = FakeConnection()

    with pytest.raises(IngestError):
        run(tmp_path, connection, item())

    assert connection.events == []


def test_a_document_with_blank_text_breaks_the_schema_and_is_not_written(tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document(text_raw="   ")]))
    connection = FakeConnection()

    with pytest.raises(IngestError):
        run(tmp_path, connection, item())

    assert "INSERT INTO documents" not in " ".join(kinds(connection))
