"""Evidence boxes (where each value was read) and behaviour on degraded scans."""
import json

import pytest

from app import config
from app.extract import extract
from scripts.eval_extraction import degrade
from tests.test_scenarios import MANIFEST, run_sample  # noqa: F401

LOCATABLE = ["vendor_name", "tax_id", "invoice_number", "invoice_date", "po_number", "subtotal", "tax", "total"]


def test_every_read_value_is_located_on_the_page(fresh):
    missing = []
    for s in [m for m in MANIFEST if m['category'] != 'not_invoice']:
        ex = extract((config.SAMPLES_DIR / s["file"]).read_bytes(), use_llm=False)
        for f in LOCATABLE:
            fv = ex.fields.get(f)
            if fv and fv.value is not None and fv.source != "assumed" and not fv.box:
                missing.append((s["id"], f))
    assert not missing, missing


def test_total_box_sits_on_the_total_row(fresh):
    ex = extract((config.SAMPLES_DIR / "happy_clean.pdf").read_bytes(), use_llm=False)
    tot, sub = ex.fields["total"].box, ex.fields["subtotal"].box
    assert tot["y"] > sub["y"] and 0 <= tot["x"] <= 1 and 0 < tot["w"] < 0.5


def test_boxes_survive_a_reprocess_but_corrected_fields_lose_theirs(fresh):
    from app import pipeline
    first = run_sample("missing_fields")
    ex = first["result"]["extraction"]
    assert ex["fields"]["total"]["box"]
    child = pipeline.run_sync(filename=first["filename"], prior_extraction=ex,
                              corrections={"invoice_number": "INV-BL-9999"}, parent=first["id"], sample_id=first["sample_id"])
    f = child["result"]["extraction"]["fields"]
    assert f["total"]["box"] and f["invoice_number"]["box"] is None


@pytest.mark.parametrize("sample", MANIFEST, ids=lambda s: s["id"])
def test_degraded_scan_never_causes_an_unsafe_approval(fresh, sample):
    """Noise can make the process more cautious, but must never turn a bad invoice into a payment."""
    from app import pipeline
    pdf = degrade((config.SAMPLES_DIR / sample["file"]).read_bytes(), seed=MANIFEST.index(sample))
    run = pipeline.run_sync(filename=sample["file"], pdf_bytes=pdf, sample_id=sample["id"])
    paid = {"APPROVED", "APPROVED_WITH_NOTE"}
    if sample["expected_decision"] not in paid:
        assert run["decision"] not in paid, f'{sample["id"]}: {run["decision"]}'
