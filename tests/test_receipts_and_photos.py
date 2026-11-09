import io
import time

from fastapi.testclient import TestClient
from PIL import Image

from app import config, db, extract, images, main

client = TestClient(main.app)

RECEIPT = """Main Street Restaurant
Merchant ID: 9hqjxvufdr  Terminal ID: 11111
Transaction ID: #e6d598ef  Type: CREDIT
Number: XXXXXXXXXXXX0041  Entry Mode: Swiped  Card Type: DISCOVER
Response: APPROVED  Approval Code: 819543
Total USD$ 29.01  THANK YOU"""


def _wait(rid):
    for _ in range(150):
        r = db.get_run(rid)
        if r["status"] != "running":
            return r
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def test_receipt_text_is_detected_and_an_invoice_that_mentions_visa_is_not():
    assert len(extract.detect_receipt(RECEIPT)) >= 3
    assert extract.detect_receipt("INVOICE INV-1\nBill to: Larkspur\nWe accept Visa and Mastercard. Total 100.00") == []
    assert extract.detect_receipt("Thank you for your business. Total 10.00") == []


def test_the_receipt_sample_is_refused_and_later_checks_are_skipped(fresh, monkeypatch):
    monkeypatch.setattr("app.config.STAGE_DELAY", 0)
    rid = client.post("/api/runs/sample/card_receipt").json()["run_id"]
    r = _wait(rid)
    d = r["result"]
    assert r["decision"] == "REJECTED" and d["headline"] == "Not an invoice" and "RECEIPT_NOT_INVOICE" in r["reason_codes"]
    assert "already paid" in d["explanation"]
    assert not any(c["code"] in ("VENDOR_NOT_APPROVED", "MISSING_INVOICE_NUMBER", "PO_NOT_FOUND") for c in d["checks"])


def test_a_jpeg_photo_can_be_uploaded_and_is_read_like_a_scan(fresh, monkeypatch):
    monkeypatch.setattr("app.config.STAGE_DELAY", 0)
    jpg = (config.BASE_DIR / "assets" / "card_receipt.jpg").read_bytes()
    rid = client.post("/api/runs", files={"file": ("photo.jpg", jpg, "image/jpeg")}).json()["run_id"]
    r = _wait(rid)
    codes = [c["code"] for c in r["result"]["checks"]]
    assert "PHOTO_CONVERTED" in codes and r["result"]["extraction"]["method"] == "ocr" and r["decision"] == "REJECTED"


def test_image_conversion_handles_png_with_transparency_and_rejects_junk():
    buf = io.BytesIO()
    Image.new("RGBA", (200, 100), (255, 255, 255, 0)).save(buf, "PNG")
    assert images.to_pdf(buf.getvalue()).startswith(b"%PDF") and images.sniff(buf.getvalue()) == "png"
    import pytest
    with pytest.raises(ValueError):
        images.to_pdf(b"\xff\xd8\xff not really a jpeg")


def test_a_corrupt_photo_gets_a_clear_error_not_a_crash(fresh):
    r = client.post("/api/runs", files={"file": ("x.jpg", b"\xff\xd8\xff" + b"0" * 50, "image/jpeg")})
    assert r.status_code == 400 and "could not be opened" in r.json()["detail"]
