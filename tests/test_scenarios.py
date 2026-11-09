import time
import json

import pytest

from app import config, db, pipeline
from app.extract import extract

MANIFEST = json.loads((config.SAMPLES_DIR / "manifest.json").read_text())["samples"]
INVOICES = [s for s in MANIFEST if s["category"] != "not_invoice"]
BY_ID = {s["id"]: s for s in MANIFEST}


def run_sample(sid, **kw):
    s = BY_ID[sid]
    pdf = (config.SAMPLES_DIR / s["file"]).read_bytes()
    return pipeline.run_sync(filename=s["file"], pdf_bytes=pdf, sample_id=sid, source="sample", **kw)


def codes(run):
    return {c["code"] for c in run["result"]["checks"]}


@pytest.mark.parametrize("sid", [s["id"] for s in MANIFEST])
def test_every_sample_reaches_expected_decision(fresh, sid):
    s = BY_ID[sid]
    run = run_sample(sid)
    assert run["status"] == "done"
    assert run["decision"] == s["expected_decision"], run["summary"]
    missing = set(s["expected_codes"]) - codes(run)
    assert not missing, f"expected codes not raised: {missing}"


def test_extraction_accuracy_against_ground_truth(fresh):
    fields = ["vendor_name", "tax_id", "bank_last4", "invoice_number", "invoice_date", "po_number", "total"]
    right = total = 0
    for s in INVOICES:
        ex = extract((config.SAMPLES_DIR / s["file"]).read_bytes(), use_llm=False)
        for f in fields:
            total += 1
            right += ex.get(f) == s["truth"][f]
    assert right / total >= 0.97, f"{right}/{total}"


def test_scanned_invoice_actually_uses_ocr(fresh):
    ex = extract((config.SAMPLES_DIR / "scanned_implied_po.pdf").read_bytes(), use_llm=False)
    assert ex.method == "ocr" and ex.ocr_confidence > 0.7


def test_same_invoice_twice_is_caught_by_the_ledger(fresh):
    first = run_sample("happy_clean")
    second = run_sample("happy_clean")
    assert first["decision"] == "APPROVED"
    assert second["decision"] == "REJECTED" and "DUPLICATE_EXACT" in codes(second)
    assert "FILE_SEEN_BEFORE" in codes(second)


def test_split_po_balance_is_tracked_across_runs(fresh):
    over = run_sample("split_over")          # held, so it must not consume PO balance
    assert over["decision"] == "HOLD"
    assert over["result"]["cumulative"]["cumulative_after"] == 32500
    ok = run_sample("split_ok")              # still has the full 12,000 available
    assert ok["decision"] == "APPROVED"
    assert db.ledger_for_po("PO-4503")[-1]["amount"] == 12000
    again = run_sample("tolerance")          # unrelated PO, unaffected
    assert again["decision"] == "APPROVED_WITH_NOTE"


def test_held_invoice_can_be_fixed_and_reprocessed(fresh):
    held = run_sample("missing_fields")
    assert held["decision"] == "HOLD"
    fixed = pipeline.run_sync(filename="missing_fields.pdf", source="reprocess", parent=held["id"],
                              prior_extraction=held["result"]["extraction"],
                              corrections=dict(invoice_number="INV-BL-2301", invoice_date="2026-11-05"))
    assert fixed["decision"] == "APPROVED", fixed["summary"]
    assert "HUMAN_CORRECTION" in codes(fixed)


def test_garbage_file_becomes_a_clear_hold_not_a_crash(fresh):
    run = pipeline.run_sync(filename="x.pdf", pdf_bytes=b"this is not a pdf at all", source="upload")
    assert run["status"] == "done" and run["decision"] == "HOLD"
    assert "UNREADABLE_DOCUMENT" in codes(run)


def test_bank_mismatch_never_emails_the_vendor(fresh):
    run = run_sample("lookalike_bank")
    email = run["result"]["email"]
    assert run["decision"] == "ESCALATE" and email["internal"] is True
    assert "larkspur" in email["to"]


def test_decisions_always_carry_evidence(fresh):
    for s in MANIFEST:
        run = run_sample(s["id"])
        for c in run["result"]["checks"]:
            assert c["message"] and c["code"] and c["outcome"]
        assert run["result"]["explanation"]


def test_llm_crosscheck_lowers_confidence_on_disagreement(fresh):
    from app.extract import llm_crosscheck

    class Msg:
        def __init__(self, text):
            self.content = [type("B", (), dict(type="text", text=text))()]

    class Fake:
        class messages:
            @staticmethod
            def create(**kw):
                return Msg('{"vendor_name":"Northwind Industrial Supply LLC","invoice_number":"INV-NW-99999","invoice_date":"2026-11-01","po_number":"PO-4501","total":3294.0,"subtotal":3050.0,"tax":244.0}')

    ex = extract((config.SAMPLES_DIR / "happy_clean.pdf").read_bytes(), use_llm=False)
    info = llm_crosscheck(ex, client=Fake())
    assert info["status"] == "ok" and "invoice_number" in info["disagree"] and "total" in info["agree"]
    assert ex.conf("invoice_number") <= 0.5


def _fake(reply):
    class Msg:
        def __init__(self, text):
            self.content = [type("B", (), dict(type="text", text=text))()]

    class Fake:
        class messages:
            @staticmethod
            def create(**kw):
                return Msg(reply)
    return Fake()


def test_ai_reader_fills_a_field_the_rules_missed_if_it_is_on_the_page(fresh):
    from app.extract import llm_crosscheck
    ex = extract((config.SAMPLES_DIR / "happy_clean.pdf").read_bytes(), use_llm=False)
    real = ex.get("invoice_number")
    ex.fields["invoice_number"].value, ex.fields["invoice_number"].confidence = None, 0.0
    info = llm_crosscheck(ex, client=_fake('{"invoice_number":"%s"}' % real))
    assert "invoice_number" in info["filled"] and ex.get("invoice_number") == real
    assert ex.fields["invoice_number"].source == "AI reader" and 0.7 < ex.conf("invoice_number") < 0.9


def test_ai_reader_cannot_invent_a_value_that_is_not_on_the_page(fresh):
    from app.extract import llm_crosscheck
    ex = extract((config.SAMPLES_DIR / "happy_clean.pdf").read_bytes(), use_llm=False)
    ex.fields["invoice_number"].value, ex.fields["invoice_number"].confidence = None, 0.0
    info = llm_crosscheck(ex, client=_fake('{"invoice_number":"INV-ZZ-00000","total":999999.99}'))
    assert not info["filled"] and ex.get("invoice_number") is None
    assert "invoice_number" in info["ignored"]
    assert ex.get("total") != 999999.99


def test_sample_downloads(fresh):
    from fastapi.testclient import TestClient
    from app import main
    c = TestClient(main.app)
    assert len(c.get("/api/samples").json()) == 8
    assert c.get("/api/samples/happy_clean/pdf").content.startswith(b"%PDF")
    z = c.get("/api/samples/download.zip")
    import io, zipfile
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert len(names) == 9 and "README.txt" in names


def test_messy_samples_are_read_by_ocr(fresh):
    from fastapi.testclient import TestClient
    from app import main
    c = TestClient(main.app)
    with c:
        by = {s["id"]: s for s in c.get("/api/samples").json()}
        assert by["messy_photo"]["scanned"] and by["messy_fax"]["scanned"]
        for sid, want in (("messy_photo", "APPROVED"), ("messy_fax", "HOLD")):
            rid = c.post(f"/api/runs/sample/{sid}").json()["run_id"]
            for _ in range(120):
                run = c.get(f"/api/runs/{rid}").json()
                if run["status"] != "running":
                    break
                time.sleep(1)
            assert run["decision"] == want, (sid, run["decision"], run["reason_codes"])
