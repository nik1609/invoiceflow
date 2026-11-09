import pytest
from fastapi.testclient import TestClient

from app import db, main

client = TestClient(main.app)
J = {"Content-Type": "application/json"}


@pytest.fixture()
def clean(fresh):
    db.execute("DELETE FROM custom_pos")
    db.execute("DELETE FROM custom_vendors")
    yield
    db.execute("DELETE FROM custom_pos")
    db.execute("DELETE FROM custom_vendors")


def _run(po, amount=None):
    rid = client.post(f"/api/purchase-orders/{po}/test-invoice", json={"amount": amount} if amount else {}).json()["run_id"]
    from app import pipeline
    for _ in range(100):
        r = db.get_run(rid)
        if r["status"] != "running":
            return r
        import time
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def test_dashboard_starts_empty_and_counts_only_real_runs(clean):
    assert client.get("/api/stats").json()["total"] == 0
    assert client.get("/api/runs").json() == []


def test_added_po_is_matched_by_the_next_invoice(clean, monkeypatch):
    monkeypatch.setattr("app.config.STAGE_DELAY", 0)
    v = client.post("/api/vendors", json=dict(name="Acme Tools LLC", tax_id="12-3456789", bank_last4="4455")).json()
    assert client.post("/api/purchase-orders", json=dict(po_number="po 4700", vendor_id=v["id"], description="Hand tools",
                                                         total=8000, expiry_date="2099-01-01")).json()["po_number"] == "PO-4700"
    ok = _run("PO-4700")
    assert ok["decision"] == "APPROVED" and ok["po_number"] == "PO-4700"
    po = next(p for p in client.get("/api/purchase-orders").json() if p["po_number"] == "PO-4700")
    assert po["added"] and po["billed"] == 8000 and po["remaining"] == 0
    over = _run("PO-4700", 9500)
    assert over["decision"] == "REJECTED" and "OVER_PO_LIMIT" in over["reason_codes"]


def test_po_form_validation(clean):
    base = dict(vendor_id="V001", description="x", total=100, expiry_date="2099-01-01")
    assert client.post("/api/purchase-orders", json=dict(base, po_number="X1")).status_code == 400
    assert client.post("/api/purchase-orders", json=dict(base, po_number="PO-4501")).status_code == 409
    assert client.post("/api/purchase-orders", json=dict(base, po_number="PO-4800", total=0)).status_code == 400
    assert client.post("/api/purchase-orders", json=dict(base, po_number="PO-4800", vendor_id="V006")).status_code == 400
    assert client.post("/api/vendors", json=dict(name="Bad", tax_id="123", bank_last4="1")).status_code == 400
    assert client.post("/api/vendors", json=dict(name="Dup", tax_id="84-1920374", bank_last4="1234")).status_code == 409


def test_reset_keeps_added_pos_but_clears_runs(clean, monkeypatch):
    monkeypatch.setattr("app.config.STAGE_DELAY", 0)
    v = client.post("/api/vendors", json=dict(name="Keep Me Inc", tax_id="11-2223334", bank_last4="9999")).json()
    client.post("/api/purchase-orders", json=dict(po_number="PO-4900", vendor_id=v["id"], description="x", total=500, expiry_date="2099-01-01"))
    _run("PO-4900")
    assert client.post("/api/admin/reset").status_code == 200
    assert client.get("/api/runs").json() == []
    po = next(p for p in client.get("/api/purchase-orders").json() if p["po_number"] == "PO-4900")
    assert po["billed"] == 0


def test_download_invoice_for_a_po_and_upload_it(clean):
    v = client.post("/api/vendors", json=dict(name="Acme Cloud Services", tax_id="55-1234567", bank_last4="9911", email="ap@acme.example")).json()
    po = client.post("/api/purchase-orders", json=dict(po_number="PO-9001", vendor_id=v["id"], description="Cloud hosting", total=5000, expiry_date="2027-12-31"))
    assert po.status_code == 200
    r = client.get("/api/purchase-orders/PO-9001/invoice.pdf?amount=3000")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and "attachment" in r.headers["content-disposition"]
    assert client.get("/api/purchase-orders/PO-0000/invoice.pdf").status_code == 404
    assert client.get("/api/purchase-orders/PO-9001/invoice.pdf?amount=-5").status_code == 400
    up = client.post("/api/runs", files={"file": ("inv.pdf", r.content, "application/pdf")})
    assert up.status_code == 200


def test_delete_added_po_only(clean):
    v = client.post("/api/vendors", json=dict(name="Gone Soon Ltd", tax_id="55-7654321", bank_last4="1122", email="")).json()
    assert client.post("/api/purchase-orders", json=dict(po_number="PO-9002", vendor_id=v["id"], description="x", total=100, expiry_date="2027-12-31")).status_code == 200
    assert client.delete("/api/purchase-orders/PO-4501").status_code == 403
    assert client.delete("/api/purchase-orders/PO-0000").status_code == 404
    assert client.delete("/api/purchase-orders/PO-9002").status_code == 200
    assert "PO-9002" not in [p["po_number"] for p in client.get("/api/purchase-orders").json()]
    assert client.post("/api/purchase-orders", json=dict(po_number="PO-9002", vendor_id=v["id"], description="x", total=100, expiry_date="2027-12-31")).status_code == 200
