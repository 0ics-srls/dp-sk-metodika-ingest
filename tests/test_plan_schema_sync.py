"""plan.schema.json is one file kept in two class repos (convert writes the plan, ingest reads it): the copies must be equal.

The sibling repo is looked up lazily; where it is absent (the ProcMon test container holds this repo only) the comparison is skipped and the file is
still checked on its own.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from builders import coverage, document, new_plan


def _own() -> Path:
    return Path(__file__).resolve().parent.parent / "plan.schema.json"


def _sibling(relative: str) -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "dp-sk-metodika-convert" / relative
        if candidate.is_file():
            return candidate
    return None


def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_own().read_text(encoding="utf-8")))


def test_the_schema_is_valid() -> None:
    Draft202012Validator.check_schema(json.loads(_own().read_text(encoding="utf-8")))


def test_the_two_repos_carry_the_same_plan_schema() -> None:
    other = _sibling("plan.schema.json")
    if other is None:
        pytest.skip("dp-sk-metodika-convert is not next to this repo")

    assert json.loads(other.read_text(encoding="utf-8")) == json.loads(_own().read_text(encoding="utf-8"))


def test_a_plan_of_the_contract_validates() -> None:
    plan = new_plan([document(), document(source_id="nulls", legacy_doc_id=None, section=None, numero=None, number=None, dept=None, anno=None, data=None,
                                           oggetto=None, source_url=None, pdf_bytes=None, pdf_pages=None, pdf_created=None, text_source="ocr")],
                    [{"fonte": "financnasprava", "item_ref": "x", "reason": "no_pdf"}], cover=coverage(note="a;b"), warnings=["w"])

    assert list(_validator().iter_errors(plan)) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(planVersion=2),
        lambda p: p["documents"][0].update(text_source="scan"),
        lambda p: p["documents"][0].update(text_raw=" \n"),
        lambda p: p["documents"][0].pop("number"),
        lambda p: p["documents"][0].update(extra=1),
        lambda p: p["documents"][0].update(fonte="ade-portale"),
        lambda p: p["coverage"].pop("note"),
        lambda p: p.pop("warnings"),
    ],
)
def test_a_broken_plan_is_refused(mutate: object) -> None:
    plan = new_plan([document()])
    mutate(plan)  # type: ignore[operator]

    assert list(_validator().iter_errors(plan))
