"""The ``execute`` action: loads the plan file of each item (``<dirIn>/<safe itemId>.plan.json``), one transaction per item."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from jsonschema import Draft202012Validator

from dp_sk_metodika_ingest.db import role_of
from dp_sk_metodika_ingest.errors import CONFIG_ERROR, SYSTEM_ERROR, TRANSIENT_ERROR, DbConnectError, IngestError
from dp_sk_metodika_ingest.repository import TABLES, clear_skipped, write_coverage, write_document, write_skipped

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager

    from dp_sk_metodika_ingest.repository import Cursor

KINDS = ("fs", "pdf")
_SCHEMA = re.compile(r"^ds_[a-z0-9_]+$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class Connection(Protocol):
    def transaction(self, *, force_rollback: bool = False) -> AbstractContextManager[Any]: ...

    def cursor(self) -> AbstractContextManager[Cursor]: ...

    def execute(self, sql: Any, params: Any = None) -> Any: ...

    def close(self) -> None: ...



def safe_name(item_id: str) -> str:
    """File name stem of an item id (identical to the grab and convert classes)."""
    return _UNSAFE.sub("_", item_id.replace(" ", "-"))


def _plan_validator() -> Draft202012Validator:
    """The plan schema shipped next to main.py (repo root, /app in the image); read when first needed, never at import."""
    path = Path(__file__).resolve().parents[2] / "plan.schema.json"
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def _validate(params: dict[str, Any]) -> tuple[list[dict[str, Any]], Path, str, str, bool]:
    items = params.get("items")
    if not isinstance(items, list) or not items:
        raise IngestError(CONFIG_ERROR, "items must be a non-empty array")
    for entry in items:
        meta = entry.get("meta") if isinstance(entry, dict) else None
        if not isinstance(entry, dict) or not entry.get("key") or not isinstance(meta, dict) or meta.get("kind") not in KINDS:
            raise IngestError(CONFIG_ERROR, f"item needs a key and meta.kind one of {list(KINDS)}: {entry!r}")
        if not isinstance(meta.get("itemId"), str) or not meta["itemId"]:
            raise IngestError(CONFIG_ERROR, f"item {entry['key']}: meta.itemId is required")
    directory, conn_string, schema = params.get("dirIn"), params.get("connString"), params.get("schema")
    if not isinstance(directory, str) or not directory:
        raise IngestError(CONFIG_ERROR, "dirIn is required")
    if not isinstance(conn_string, str) or not conn_string:
        raise IngestError(CONFIG_ERROR, "connString is required")
    if not isinstance(schema, str) or not _SCHEMA.match(schema):
        raise IngestError(CONFIG_ERROR, "schema must be a dataset schema name (ds_...)")
    dry_run = params.get("dryRun")  # JSON null = absent
    if dry_run is not None and not isinstance(dry_run, bool):
        raise IngestError(CONFIG_ERROR, "dryRun must be a boolean")
    return items, Path(directory), conn_string, schema, bool(dry_run)


def _load(entry: dict[str, Any], directory: Path) -> dict[str, Any]:
    """The validated plan of the item, before anything touches the database."""
    item_id = entry["meta"]["itemId"]
    path = directory / f"{safe_name(item_id)}.plan.json"
    if not path.is_file():
        msg = f"no plan file for the item at {path}"
        raise FileNotFoundError(msg)
    plan = json.loads(path.read_text(encoding="utf-8"))
    errors = sorted(_plan_validator().iter_errors(plan), key=lambda e: list(e.path))
    if errors:
        msg = f"{path.name} breaks the plan schema: {errors[0].message[:200]}"
        raise ValueError(msg)
    if plan["itemId"] != item_id:
        msg = f"{path.name} belongs to item {plan['itemId']}, not to {item_id}"
        raise ValueError(msg)
    return plan  # type: ignore[no-any-return]


def _write(cursor: Cursor, plan: dict[str, Any]) -> dict[str, int]:
    counts = dict.fromkeys((*TABLES, "passages_kept"), 0)
    source_ids: dict[str, list[str]] = {}
    for doc in plan["documents"]:
        for name, value in write_document(cursor, doc).items():
            counts[name] += value
        source_ids.setdefault(doc["fonte"], []).append(doc["source_id"])
    for fonte, ids in source_ids.items():
        clear_skipped(cursor, fonte, ids)
    counts["skipped"] += write_skipped(cursor, [(s["fonte"], s["item_ref"], s["reason"]) for s in plan["skipped"]])
    counts["coverage"] += write_coverage(cursor, plan["coverage"])
    counts["skipped_documents"] = len(plan["skipped"])
    counts["warnings"] = len(plan["warnings"])
    return counts


@dataclass(frozen=True)
class _Run:
    """The validated settings of one execute."""

    connection: Connection
    schema: str
    directory: Path
    dry_run: bool


def _write_item(run: _Run, plan: dict[str, Any]) -> dict[str, int]:
    """One transaction for the item; a dry run does all the writes (so the counts are real) and rolls them back."""
    with run.connection.transaction(force_rollback=run.dry_run):
        run.connection.execute(f"SET search_path TO {run.schema}, public")  # schema is validated against ^ds_[a-z0-9_]+$
        with run.connection.cursor() as cursor:
            return _write(cursor, plan)


def _items(run: _Run, items: list[dict[str, Any]], on_progress: Callable[[int, str], None]) -> Iterator[tuple[dict[str, Any], dict[str, int] | Exception]]:
    for index, entry in enumerate(items):
        try:
            yield entry, _write_item(run, _load(entry, run.directory))
        except Exception as error:  # noqa: BLE001 - any failure of one item is reported, the next item still runs
            yield entry, error
        on_progress(int((index + 1) * 100 / len(items)), f"{index + 1}/{len(items)} {entry['key']}")


def run_ingest(params: dict[str, Any], connect: Callable[[str], Connection], on_progress: Callable[[int, str], None]) -> dict[str, Any]:
    """Ingests every item; failed items are rolled back and reported together as one system_error after the good ones were committed."""
    items, directory, conn_string, schema, dry_run = _validate(params)
    try:
        connection = connect(conn_string)
    except (OSError, DbConnectError) as ex:
        msg = f"cannot connect to the dataset database as role {role_of(conn_string)} ({type(ex).__name__})"
        raise IngestError(TRANSIENT_ERROR, msg) from None
    metrics: dict[str, int] = {"items": 0, "skipped_documents": 0, "warnings": 0, "passages_kept": 0}
    metrics.update({f"written.{schema}.{table}": 0 for table in TABLES})
    loaded: list[str] = []
    failures: list[str] = []
    try:
        for entry, outcome in _items(_Run(connection, schema, directory, dry_run), items, on_progress):
            if isinstance(outcome, Exception):
                failures.append(f"{entry['key']} ({type(outcome).__name__}: {outcome})")
                continue
            metrics["items"] += 1
            for name in ("skipped_documents", "warnings", "passages_kept"):
                metrics[name] += outcome[name]
            for table in TABLES:
                metrics[f"written.{schema}.{table}"] += outcome[table]
            loaded.append(entry["key"])
    finally:
        connection.close()
    if failures:
        msg = f"{len(failures)} of {len(items)} items failed: {'; '.join(failures)}"
        raise IngestError(SYSTEM_ERROR, msg)
    return {"metrics": metrics, "loaded": loaded}
