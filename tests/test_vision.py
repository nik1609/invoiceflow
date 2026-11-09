import json

import pytest

from app import config, extract, vision
from app.extract import Extraction, FieldValue


def _ex(**fields):
    f = {k: FieldValue(v[0], v[1], "rules") for k, v in fields.items()}
    return Extraction(method="ocr", ocr_confidence=0.6, page_count=1, text="Acme Tools LLC INV-20931 Total 1,180.00 Date 16 Oct 2026", fields=f)


def test_digital_pdfs_never_need_vision():
    ex = _ex(total=(10.0, 0.9))
    ex.method = "digital"
    assert vision.needs_vision(ex) is None


def test_clean_scan_does_not_trigger_vision():
    ex = _ex(**{n: ("x", 0.95) for n in config.CRITICAL_FIELDS})
    ex.ocr_confidence = 0.93
    assert vision.needs_vision(ex) is None


def test_low_ocr_or_missing_critical_field_triggers_vision():
    ex = _ex(**{n: ("x", 0.95) for n in config.CRITICAL_FIELDS})
    ex.ocr_confidence = 0.7
    assert "70%" in vision.needs_vision(ex)
    ex.ocr_confidence = 0.95
    del ex.fields["total"]
    assert "total" in vision.needs_vision(ex)


def test_values_are_cleaned_and_junk_is_dropped():
    d = vision.parse('Sure! {"vendor_name":" Acme ","invoice_number":null,"invoice_date":"16/10/2026","subtotal":"$1,000.00","tax":"abc","total":1180}')
    assert d["vendor_name"] == "Acme" and d["invoice_number"] is None and d["invoice_date"] is None
    assert d["subtotal"] == 1000.0 and d["tax"] is None and d["total"] == 1180.0
    with pytest.raises(vision.VisionError):
        vision.parse("no json here")


def test_agreement_raises_confidence_and_conflict_lowers_it():
    ex = _ex(invoice_number=("INV-20931", 0.9), total=(1180.0, 0.9))
    r = vision.apply(ex, dict(invoice_number="inv 20931", total=1100.0))
    assert "invoice_number" in r["confirmed"] and ex.conf("invoice_number") > 0.9
    assert "total" in r["conflicts"] and ex.conf("total") <= 0.5


def test_a_value_the_page_backs_up_is_trusted_and_one_it_does_not_is_sent_to_a_person():
    ex = _ex(invoice_number=("INV-2O931", 0.4))
    r = vision.apply(ex, dict(invoice_number="INV-20931", vendor_name="Totally Different Name", subtotal=1000.0, tax=180.0, total=1180.0))
    assert ex.get("invoice_number") == "INV-20931" and ex.conf("invoice_number") >= config.MIN_FIELD_CONFIDENCE   # close to what OCR saw
    assert ex.get("vendor_name") == "Totally Different Name" and ex.conf("vendor_name") < config.MIN_FIELD_CONFIDENCE  # nothing backs it up
    assert "vendor_name" in r["unconfirmed"] and ex.conf("total") >= config.MIN_FIELD_CONFIDENCE                       # the numbers add up


def test_numbers_that_do_not_add_up_are_not_trusted():
    ex = _ex()
    ex.text = "no amounts here"
    r = vision.apply(ex, dict(subtotal=1000.0, tax=180.0, total=1500.0))
    assert set(r["unconfirmed"]) == {"subtotal", "tax", "total"}


def test_a_failed_call_is_reported_and_never_raises(monkeypatch):
    def boom(images, prompt):
        raise vision.VisionError("HTTP 404: model not found")
    monkeypatch.setattr(vision, "_call", boom)
    monkeypatch.setattr(extract, "llm_provider", lambda: "groq")
    out = vision.run(open(config.SAMPLES_DIR / "scanned_implied_po.pdf", "rb").read(), _ex(), "test")
    assert out["status"] == "error" and "404" in out["reason"]


def test_a_scanned_invoice_goes_through_the_vision_step_and_shows_in_the_trace(fresh, monkeypatch):
    from fastapi.testclient import TestClient
    from app import db, main
    import time
    monkeypatch.setattr("app.config.STAGE_DELAY", 0)
    monkeypatch.setattr(config, "VISION_MIN_OCR", 1.01)               # treat every scan as hard to read
    monkeypatch.setattr(extract, "llm_provider", lambda: "groq")
    monkeypatch.setattr(extract, "_ask_groq", lambda *a, **k: "{}")
    seen = {}
    monkeypatch.setattr(vision, "_call", lambda images, prompt: seen.setdefault("n", len(images)) and json.dumps(dict(vendor_name="Zzz Unknown Co")))
    c = TestClient(main.app)
    rid = c.post("/api/runs/sample/scanned_implied_po").json()["run_id"]
    for _ in range(150):
        r = db.get_run(rid)
        if r["status"] != "running":
            break
        time.sleep(0.2)
    codes = [x["code"] for x in r["result"]["checks"]]
    assert seen["n"] >= 1 and "VISION_READ" in codes


def _server(responses):
    """A tiny local stand-in for the provider so the real HTTP code path is exercised."""
    import http.server
    import threading
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.headers["Authorization"], body))
            code, payload = responses[min(len(seen) - 1, len(responses) - 1)]
            self.send_response(code)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_request_shape_and_retry_on_rate_limit(monkeypatch):
    ok = {"choices": [{"message": {"content": '{"vendor_name": "Acme"}'}}]}
    srv, seen = _server([(429, {"error": {"message": "slow down"}}), (200, ok)])
    monkeypatch.setenv("GROQ_BASE_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_000000000000")
    monkeypatch.setattr(extract, "llm_provider", lambda: "groq")
    monkeypatch.setattr(vision.time, "sleep", lambda s: None)
    imgs = vision.render_pages(open(config.SAMPLES_DIR / "scanned_implied_po.pdf", "rb").read())
    assert vision._call(imgs, "read it") == '{"vendor_name": "Acme"}'
    auth, body = seen[-1]
    assert len(seen) == 2 and auth == "Bearer gsk_test_key_000000000000" and body["model"] == vision.VISION_DEFAULT_MODEL
    parts = body["messages"][0]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,") and len(imgs[0]) < 1_500_000
    srv.shutdown()


def test_a_wrong_model_name_fails_fast_with_the_providers_message(monkeypatch):
    srv, seen = _server([(404, {"error": {"message": "The model `x` does not exist"}})])
    monkeypatch.setenv("GROQ_BASE_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_key_000000000000")
    monkeypatch.setattr(extract, "llm_provider", lambda: "groq")
    with pytest.raises(vision.VisionError) as e:
        vision._call(["AAAA"], "read it")
    assert len(seen) == 1 and "404" in str(e.value) and "does not exist" in str(e.value)
    srv.shutdown()


def test_an_empty_ai_answer_is_not_a_disagreement(monkeypatch):
    from types import SimpleNamespace
    ex = _ex(total=(1180.0, 0.9), invoice_number=("INV-20931", 0.9))
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **k: SimpleNamespace(content=[SimpleNamespace(type="text", text="{}")])))
    out = extract.llm_crosscheck(ex, client=client)
    assert out["status"] == "ok" and not out["disagree"] and ex.conf("total") == 0.9
