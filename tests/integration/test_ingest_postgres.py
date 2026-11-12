"""Real Postgres + pgvector (Testcontainers) with the package migrations 0001..0003 applied; no Jina call (embeddings are set by SQL).

The container is a throwaway pgvector image; the ``unaccent`` extension is created by the fixture, as the Host does in production.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from testcontainers.community.postgres import PostgresContainer

from builders import ITEM_ID, LONG_TEXT, coverage, document, item, new_plan, write_plan
from dp_sk_metodika_ingest.db import connect
from dp_sk_metodika_ingest.errors import IngestError
from dp_sk_metodika_ingest.ingest import run_ingest

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

SCHEMA = "ds_sk_metodika"
VECTOR = "[" + ",".join(["0.25"] * 1024) + "]"
NBS_ID = "pdf:nbs-metodicke:all"


def migrations_dir() -> Path:
    """The package migrations next to this class repo; resolved lazily so collecting the module in a container (no sibling repo) cannot fail."""
    for parent in Path(__file__).resolve().parents:
        for candidate in (parent / "dp-sk-metodika" / "schema" / "migrations", parent.parent / "dp-sk-metodika" / "schema" / "migrations"):
            if candidate.is_dir():
                return candidate
    msg = "package migrations (dp-sk-metodika/schema/migrations) not found next to this repo"
    raise FileNotFoundError(msg)


class Db:
    def __init__(self, container: PostgresContainer) -> None:
        self.dsn = container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        self.conn_string = (
            f"Host={container.get_container_host_ip()};Port={container.get_exposed_port(5432)};"
            f"Database={container.dbname};Username={container.username};Password={container.password}"
        )

    def scalar(self, sql: str) -> Any:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            row = conn.execute(sql).fetchone()
            assert row is not None
            return row[0]

    def execute(self, sql: str) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(sql)

    def ingest(self, directory: Path, *items: dict[str, Any], **extra: Any) -> dict[str, Any]:
        params = {"items": list(items), "dirIn": str(directory), "connString": self.conn_string, "schema": SCHEMA, **extra}
        return run_ingest(params, connect, lambda *_: None)

    def count(self, table: str, where: str = "true") -> int:
        return int(self.scalar(f"SELECT count(*) FROM {SCHEMA}.{table} WHERE {where}"))


@pytest.fixture(scope="module")
def container() -> Iterator[PostgresContainer]:
    with PostgresContainer("pgvector/pgvector:pg17", username="lex", password="pw", dbname="lex") as pg:
        yield pg


@pytest.fixture
def db(container: PostgresContainer) -> Db:
    database = Db(container)
    database.execute("CREATE EXTENSION IF NOT EXISTS vector")
    database.execute("CREATE EXTENSION IF NOT EXISTS unaccent")  # the Host provides it in production (allow-list vector, unaccent)
    database.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    database.execute(f"CREATE SCHEMA {SCHEMA}")
    with psycopg.connect(database.dsn, autocommit=True) as conn:
        conn.execute(f"SET search_path TO {SCHEMA}, public")
        for migration in sorted(migrations_dir().glob("*.sql")):
            conn.execute(migration.read_text(encoding="utf-8"))
    return database


def three_documents() -> list[dict[str, Any]]:
    return [
        document(source_id="doc_a", text_raw=LONG_TEXT),
        document(source_id="doc_b", numero="11/DZPaU/2026/MP", text_raw=LONG_TEXT.replace("pridanej", "pridanej hodnoty")),
        document(source_id="doc_c", numero=None, anno=None, data=None, oggetto=None, text_raw="Krátky text bez čísla a roku. " * 5),
    ]


def test_first_ingest_writes_documents_passages_skipped_and_coverage(db: Db, tmp_path: Path) -> None:
    skipped = [{"fonte": "financnasprava", "item_ref": "scan_1", "reason": "ocr_empty"}]
    write_plan(tmp_path, new_plan(three_documents(), skipped, cover=coverage(listed=4, taken=3, skipped=1, note="zip_archives_only")))

    result = db.ingest(tmp_path, item())

    assert result["metrics"][f"written.{SCHEMA}.documents"] == 3
    assert db.count("documents") == 3
    assert db.count("passages") > 3
    assert db.count("passages", "embedding IS NULL") == db.count("passages")
    assert db.count("skipped") == 1
    assert db.scalar(f"SELECT note FROM {SCHEMA}.coverage") == "zip_archives_only"
    assert db.count("documents", "numero IS NULL AND anno IS NULL AND data IS NULL") == 1
    assert db.count("documents", "search_vector @@ websearch_to_tsquery('ds_sk_metodika.sk_unaccent'::regconfig, 'dane')") == 0, "no stemming"
    assert db.count("documents", "search_vector @@ websearch_to_tsquery('ds_sk_metodika.sk_unaccent'::regconfig, 'danovy subjekt')") == 2, "diacritics are folded"


def test_loading_the_same_plan_twice_keeps_every_count_and_the_embeddings(db: Db, tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan(three_documents(), [{"fonte": "financnasprava", "item_ref": "scan_1", "reason": "ocr_empty"}]))
    db.ingest(tmp_path, item())
    db.execute(f"UPDATE {SCHEMA}.passages SET embedding = '{VECTOR}'")
    counts = {t: db.count(t) for t in ("documents", "passages", "skipped", "coverage")}
    ids = db.scalar(f"SELECT string_agg(id::text, ',' ORDER BY source_id) FROM {SCHEMA}.documents")

    result = db.ingest(tmp_path, item())

    assert {t: db.count(t) for t in counts} == counts
    assert db.scalar(f"SELECT string_agg(id::text, ',' ORDER BY source_id) FROM {SCHEMA}.documents") == ids
    assert db.count("passages", "embedding IS NOT NULL") == counts["passages"]
    assert result["metrics"][f"written.{SCHEMA}.passages"] == 0
    assert result["metrics"]["passages_kept"] == counts["passages"]


def test_one_changed_text_replaces_its_passages_and_the_others_keep_their_vectors(db: Db, tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan(three_documents()))
    db.ingest(tmp_path, item())
    db.execute(f"UPDATE {SCHEMA}.passages SET embedding = '{VECTOR}'")
    old_ids = db.scalar(f"SELECT string_agg(p.id::text, ',' ORDER BY p.id) FROM {SCHEMA}.passages p JOIN {SCHEMA}.documents d ON d.id = p.document_id WHERE d.source_id = 'doc_a'")
    others = db.count("passages p", f"EXISTS (SELECT 1 FROM {SCHEMA}.documents d WHERE d.id = p.document_id AND d.source_id <> 'doc_a')")

    changed = three_documents()
    changed[0]["text_raw"] = "Úplne nový a podstatne kratší text prvého dokumentu. " * 4
    write_plan(tmp_path, new_plan(changed))
    db.ingest(tmp_path, item())

    assert db.count("passages p", f"EXISTS (SELECT 1 FROM {SCHEMA}.documents d WHERE d.id = p.document_id AND d.source_id = 'doc_a')") == 1
    assert db.count("passages p", f"embedding IS NULL AND EXISTS (SELECT 1 FROM {SCHEMA}.documents d WHERE d.id = p.document_id AND d.source_id = 'doc_a')") == 1
    new_ids = db.scalar(f"SELECT string_agg(p.id::text, ',' ORDER BY p.id) FROM {SCHEMA}.passages p JOIN {SCHEMA}.documents d ON d.id = p.document_id WHERE d.source_id = 'doc_a'")
    assert new_ids != old_ids
    assert db.count("passages p", f"embedding IS NOT NULL AND EXISTS (SELECT 1 FROM {SCHEMA}.documents d WHERE d.id = p.document_id AND d.source_id <> 'doc_a')") == others
    assert db.count("documents") == 3


def test_a_changed_title_keeps_the_embeddings_but_moves_ingested_at(db: Db, tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document(source_id="doc_a")]))
    db.ingest(tmp_path, item())
    db.execute(f"UPDATE {SCHEMA}.passages SET embedding = '{VECTOR}'")
    stamp = db.scalar(f"SELECT ingested_at FROM {SCHEMA}.documents")

    write_plan(tmp_path, new_plan([document(source_id="doc_a", oggetto="Nový názov pokynu")]))
    db.ingest(tmp_path, item())

    assert db.scalar(f"SELECT oggetto FROM {SCHEMA}.documents") == "Nový názov pokynu"
    assert db.count("passages", "embedding IS NOT NULL") == db.count("passages") > 0
    assert db.scalar(f"SELECT ingested_at FROM {SCHEMA}.documents") > stamp


def test_skipped_and_coverage_are_replaced_not_duplicated(db: Db, tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([], [{"fonte": "financnasprava", "item_ref": "scan_1", "reason": "ocr_empty"}], cover=coverage(listed=1, taken=0, skipped=1)))
    db.ingest(tmp_path, item())

    write_plan(tmp_path, new_plan([], [{"fonte": "financnasprava", "item_ref": "scan_1", "reason": "too_large"}], cover=coverage(listed=2, taken=0, skipped=2, note="x")))
    db.ingest(tmp_path, item())

    assert db.count("skipped") == 1
    assert db.scalar(f"SELECT reason FROM {SCHEMA}.skipped") == "too_large"
    assert db.count("coverage") == 1
    assert db.scalar(f"SELECT listed FROM {SCHEMA}.coverage") == 2


def test_a_document_that_is_ingested_later_clears_its_skipped_row(db: Db, tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([], [{"fonte": "financnasprava", "item_ref": "doc_late", "reason": "ocr_unavailable"}]))
    db.ingest(tmp_path, item())
    assert db.count("skipped") == 1

    write_plan(tmp_path, new_plan([document(source_id="doc_late")]))
    db.ingest(tmp_path, item())

    assert db.count("skipped") == 0
    assert db.count("documents") == 1


def test_two_documents_with_equal_titles_but_different_source_ids_coexist(db: Db, tmp_path: Path) -> None:
    same = {"oggetto": "Príloha č. 1", "numero": None, "anno": None}
    write_plan(tmp_path, new_plan([document(source_id="files_archiv_35_Priloha_c_1", **same), document(source_id="files_archiv_36_Priloha_c_1", **same)]))

    db.ingest(tmp_path, item())

    assert db.count("documents", "oggetto = 'Príloha č. 1'") == 2


def test_two_items_of_two_fonti_in_one_run(db: Db, tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan([document(source_id="a")]))
    write_plan(tmp_path, new_plan([document(fonte="nbs-metodicke", source_id="a", ente="Národná banka Slovenska", tipo="dokument", section=None)], item_id=NBS_ID, fonte="nbs-metodicke"))

    result = db.ingest(tmp_path, item(), item(NBS_ID, "pdf"))

    assert result["loaded"] == [f"{ITEM_ID}|1", f"{NBS_ID}|1"]
    assert db.count("documents") == 2, "the same source_id under two fonti is two documents"
    assert db.count("coverage") == 2


def test_a_bad_row_rolls_the_whole_item_back_and_the_other_item_commits(db: Db, tmp_path: Path) -> None:
    broken = document(source_id="broken", text_raw="text s nulovým bajtom \u0000 ktorý postgres odmietne")
    write_plan(tmp_path, new_plan([document(source_id="good_in_broken_item"), broken], [{"fonte": "financnasprava", "item_ref": "s", "reason": "no_pdf"}]))
    write_plan(tmp_path, new_plan([document(fonte="nbs-metodicke", source_id="ok")], item_id=NBS_ID, fonte="nbs-metodicke"))

    with pytest.raises(IngestError) as caught:
        db.ingest(tmp_path, item(), item(NBS_ID, "pdf"))

    assert f"{ITEM_ID}|1" in str(caught.value)
    assert db.count("documents", "source_id = 'good_in_broken_item'") == 0, "the whole item rolled back"
    assert db.count("skipped") == 0
    assert db.count("coverage", "item_id = 'fs:metodicke-pokyny'") == 0
    assert db.count("documents", "source_id = 'ok'") == 1


def test_dry_run_leaves_the_database_untouched(db: Db, tmp_path: Path) -> None:
    write_plan(tmp_path, new_plan(three_documents(), [{"fonte": "financnasprava", "item_ref": "scan_1", "reason": "ocr_empty"}]))

    result = db.ingest(tmp_path, item(), dryRun=True)

    assert result["metrics"][f"written.{SCHEMA}.documents"] == 3
    assert (db.count("documents"), db.count("passages"), db.count("skipped"), db.count("coverage")) == (0, 0, 0, 0)
