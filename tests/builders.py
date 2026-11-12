"""Test builders: plan files (schema v1), item entries and a fake psycopg connection that records statements and transaction boundaries."""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
LONG_TEXT = "Daňový subjekt žiada o výklad ustanovenia zákona o dani z pridanej hodnoty uvedeného v metodickom pokyne finančného riaditeľstva. " * 70
ITEM_ID = "fs:metodicke-pokyny"


def safe_name(item_id: str) -> str:
    return _UNSAFE.sub("_", item_id.replace(" ", "-"))


def item(item_id: str = ITEM_ID, kind: str = "fs") -> dict[str, Any]:
    return {"key": f"{item_id}|1", "meta": {"kind": kind, "itemId": item_id, "version": "1"}}


def document(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "fonte": "financnasprava",
        "source_id": "Zverejnovanie_dok_10_DZPaU_2026_MP",
        "legacy_doc_id": "10_DZPaU_2026_MP",
        "ente": "Finančná správa SR",
        "tipo": "MP",
        "section": "metodicke-pokyny",
        "group_label": None,
        "numero": "10/DZPaU/2026/MP",
        "number": 10,
        "dept": "DZPaU",
        "anno": 2026,
        "data": "2026-03-02",
        "oggetto": "Metodický pokyn k DPH",
        "text_raw": "Metodický pokyn k uplatňovaniu zákona o DPH. Daňový subjekt postupuje podľa tohto pokynu. " * 3,
        "text_source": "pdf",
        "source_url": "https://www.financnasprava.sk/_img/pfsedit/Dokumenty_PFS/Zverejnovanie_dok/10.pdf",
        "pdf_bytes": 12345,
        "pdf_pages": 3,
        "pdf_created": "2026-03-02",
    }
    base.update(overrides)
    return base


def coverage(item_id: str = ITEM_ID, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"fonte": "financnasprava", "item_id": item_id, "listed": 3, "taken": 2, "skipped": 1, "note": None}
    base.update(overrides)
    return base


def new_plan(
    docs: list[dict[str, Any]] | None = None,
    skipped: list[dict[str, str]] | None = None,
    item_id: str = ITEM_ID,
    fonte: str = "financnasprava",
    cover: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "planVersion": 1,
        "itemId": item_id,
        "fonte": fonte,
        "documents": docs or [],
        "skipped": skipped or [],
        "coverage": cover if cover is not None else coverage(item_id, fonte=fonte),
        "warnings": warnings or [],
    }


def write_plan(directory: Path, plan: dict[str, Any]) -> Path:
    path = directory / f"{safe_name(plan['itemId'])}.plan.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


class FakeCursor:
    """Records (kind, sql, params); ``RETURNING id`` answers a deterministic uuid; ``fail_on`` makes the statement containing that text raise."""

    def __init__(self, log: list[tuple[str, str, Any]], fail_on: str | None, answers: dict[str, Any]) -> None:
        self._log = log
        self._fail_on = fail_on
        self._answers = answers
        self._last = ""

    def execute(self, sql: Any, params: Any = None) -> FakeCursor:
        text = str(sql)
        if self._fail_on and self._fail_on in text:
            msg = f"boom in {self._fail_on}"
            raise RuntimeError(msg)
        self._log.append(("execute", text, params))
        self._last = text
        return self

    def executemany(self, sql: Any, params: Any) -> None:
        if self._fail_on and self._fail_on in str(sql):
            msg = f"boom in {self._fail_on}"
            raise RuntimeError(msg)
        self._log.append(("executemany", str(sql), list(params)))

    def fetchone(self) -> tuple[Any, ...] | None:
        for marker, answer in self._answers.items():
            if marker in self._last:
                result: tuple[Any, ...] | None = answer
                return result
        return (uuid.UUID(int=len(self._log)),)


class FakeConnection:
    """Records statements and transaction boundaries. ``answers`` maps a statement fragment to the row ``fetchone`` returns (None = no row)."""

    def __init__(self, fail_on: str | None = None, answers: dict[str, Any] | None = None) -> None:
        self.log: list[tuple[str, str, Any]] = []
        self.events: list[str] = []
        self.closed = False
        self._fail_on = fail_on
        self._answers: dict[str, Any] = {"FROM documents d": None, **(answers or {})}

    @contextmanager
    def transaction(self, *, force_rollback: bool = False) -> Iterator[None]:
        self.events.append("begin")
        try:
            yield
        except BaseException:
            self.events.append("rollback")
            raise
        self.events.append("rollback" if force_rollback else "commit")

    @contextmanager
    def cursor(self) -> Iterator[FakeCursor]:
        yield FakeCursor(self.log, self._fail_on, self._answers)

    def execute(self, sql: Any, params: Any = None) -> FakeCursor:
        return FakeCursor(self.log, self._fail_on, self._answers).execute(sql, params)

    def close(self) -> None:
        self.closed = True
