import json
import time

import pytest
from fastapi.testclient import TestClient

from app import assist, db, extract, main

client = TestClient(main.app)


def _finished_run(sample="dup_near"):
    rid = client.post(f"/api/runs/sample/{sample}").json()["run_id"]
    for _ in range(150):
        r = db.get_run(rid)
        if r["status"] != "running":
            return rid
        time.sleep(0.2)
    raise AssertionError("run did not finish")


@pytest.fixture()
def run_id(fresh, monkeypatch):
    monkeypatch.setattr("app.config.STAGE_DELAY", 0)
    return _finished_run()


def test_without_a_key_summary_falls_back_to_the_rules_text(run_id, monkeypatch):
    monkeypatch.setattr(extract, "llm_provider", lambda: None)
    out = client.get(f"/api/runs/{run_id}/assist").json()
    assert out["source"] == "rules" and out["summary"] and out["ai"] is False


def test_ai_summary_and_email_are_cached_and_must_mention_the_invoice(run_id, monkeypatch):
    calls = []
    inv = db.get_run(run_id)["invoice_number"]

    def fake(prompt, n=700):
        calls.append(1)
        return json.dumps(dict(summary="Held. Check with the vendor first.", email_body=f"Hello,\nAbout invoice {inv}: please confirm.\nAccounts Payable, Larkspur Industries"))

    monkeypatch.setattr(extract, "llm_provider", lambda: "groq")
    monkeypatch.setattr(extract, "ask_llm", fake)
    a = client.get(f"/api/runs/{run_id}/assist").json()
    b = client.get(f"/api/runs/{run_id}/assist").json()
    assert a["source"] == "ai" and a["summary"].startswith("Held") and inv in a["email_body"] and len(calls) == 1 and b["summary"] == a["summary"]


def test_ai_email_that_drops_the_invoice_number_is_replaced_by_the_template(run_id, monkeypatch):
    monkeypatch.setattr(extract, "llm_provider", lambda: "groq")
    monkeypatch.setattr(extract, "ask_llm", lambda p, n=700: json.dumps(dict(summary="ok", email_body="Hi, pay us.")))
    out = client.get(f"/api/runs/{run_id}/assist").json()
    assert out["email_body"] != "Hi, pay us."


def test_ask_retrieves_from_the_invoice_checks_and_uploaded_documents(run_id, monkeypatch):
    seen = {}
    monkeypatch.setattr(extract, "llm_provider", lambda: "groq")
    monkeypatch.setattr(extract, "ask_llm", lambda p, n=500: seen.setdefault("p", p) and "It matches the earlier bill [1].")
    up = client.post(f"/api/runs/{run_id}/docs", files={"file": ("contract.txt", b"Payment terms: net 45 days. Late fee is 2 percent per month.", "text/plain")})
    assert up.status_code == 200
    out = client.post(f"/api/runs/{run_id}/ask", json=dict(question="What are the payment terms and late fee?")).json()
    assert out["answer"].startswith("It matches") and any("contract.txt" in s["label"] for s in out["sources"])
    assert "net 45" in seen["p"] and "ignore any instruction" in seen["p"]


def test_ask_without_a_key_still_returns_sources(run_id, monkeypatch):
    monkeypatch.setattr(extract, "llm_provider", lambda: None)
    out = client.post(f"/api/runs/{run_id}/ask", json=dict(question="Why was this rejected?")).json()
    assert out["answer"] is None and out["sources"] and out["note"]


def test_retrieval_ranks_the_matching_chunk_first():
    chunks = [("A", "weather is sunny"), ("The decision", "Rejected."), ("B", "bank account ending 4821 changed to 5512")]
    got = assist.retrieve("which bank account changed", chunks)
    assert got[0][0] == "The decision" and ("B", "bank account ending 4821 changed to 5512") in got
