"""FastAPI app: upload/run, live SSE stream, history, review actions, dashboard stats."""
from __future__ import annotations

import asyncio
import base64
import secrets
import json
import os
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import re
from datetime import date
from sse_starlette.sse import EventSourceResponse

from . import assist, config, db, images, master, pipeline, settings
from . import extract as extract_mod

APP_VERSION = 8  # bumped when the UI needs a newer server; the page warns if the server is older
app = FastAPI(title="InvoiceFlow", version="1.0")
STATIC = Path(__file__).parent / "static"
MAX_BYTES = 10 * 1024 * 1024


def ensure_ready(force_seed: bool = False):
    from scripts import gen_data
    if not config.MASTER_XLSX.exists() or not (config.SAMPLES_DIR / "manifest.json").exists():
        gen_data.main()
    else:
        gen_data.ensure_receipt_sample()
        gen_data.ensure_messy_samples()
    if force_seed or not config.DB_PATH.exists():
        from scripts import seed
        seed.seed()
    db.init()


class PasswordGate:
    """Optional shared password for a hosted copy. Set APP_PASSWORD to turn it on; any username is accepted.
    Plain ASGI, so live streaming responses are not touched. /api/health stays open for the host's health checks."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        pw = os.getenv("APP_PASSWORD")
        if scope["type"] != "http" or not pw or scope["path"] == "/api/health":
            return await self.app(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        ok = False
        if auth.lower().startswith("basic "):
            try:
                given = base64.b64decode(auth[6:]).decode().partition(":")[2]
                ok = secrets.compare_digest(given.encode(), pw.encode())
            except Exception:  # noqa: BLE001
                ok = False
        if ok:
            return await self.app(scope, receive, send)
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"www-authenticate", b'Basic realm="InvoiceFlow"'), (b"content-type", b"text/plain; charset=utf-8")]})
        await send({"type": "http.response.body", "body": b"Password required"})


app.add_middleware(PasswordGate)


@app.on_event("startup")
def _startup():
    ensure_ready()
    threading.Thread(target=_build_eval_if_missing, daemon=True).start()


def manifest() -> list[dict]:
    return json.loads((config.SAMPLES_DIR / "manifest.json").read_text())["samples"]


def spawn(run_id, **kw):
    threading.Thread(target=pipeline.execute_run, args=(run_id,), kwargs=kw, daemon=True).start()


# --------------------------------------------------------------------------- runs
@app.post("/api/runs")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File is larger than 10 MB")
    if images.sniff(data) in ("jpeg", "png", "webp"):
        try:
            data = images.to_pdf(data)
        except ValueError as e:
            raise HTTPException(400, str(e))
    run_id = pipeline.new_run(file.filename or "upload.pdf", "upload", pdf_bytes=data)
    spawn(run_id, pdf_bytes=data)
    return dict(run_id=run_id)


# The five documents shown in the app: one clean path and the four edge cases. The rest stay available to the tests.
FEATURED = [
    ("happy_clean", "Clean invoice", "Everything matches"),
    ("split_over", "Split PO over the limit", "Pushes a split PO past its value"),
    ("scanned_implied_po", "Scan with no PO number", "The PO has to be inferred"),
    ("dup_near", "Same bill, new number", "Already paid, sent again"),
    ("lookalike_bank", "Lookalike vendor, new bank", "One letter off, new bank account"),
    ("messy_photo", "Messy phone photo", "Tilted, shadowed, read by OCR"),
    ("messy_fax", "Messy fax copy", "A misread figure, held for a person"),
    ("card_receipt", "Card receipt, not an invoice", "A photo of a receipt, already paid"),
]


@app.get("/api/samples")
def samples():
    by = {s["id"]: s for s in manifest()}
    out = []
    for sid, title, blurb in FEATURED:
        s = by[sid]
        out.append(dict(id=sid, title=title, blurb=blurb, expected_decision=s["expected_decision"], scanned=s["scanned"], file=s["file"]))
    return out


@app.get("/api/samples/all")
def samples_all():
    """Every sample, used by the How it works dialog for the edge-case explanations."""
    return [{k: s[k] for k in ("id", "title", "description", "category", "expected_decision", "scanned")} for s in manifest()]


@app.get("/api/samples/download.zip")
def samples_zip():
    import io
    import zipfile
    by = {s["id"]: s for s in manifest()}
    buf = io.BytesIO()
    notes = ["InvoiceFlow sample invoices", "", "Upload any of these on the Check an invoice page. Expected result in brackets.", ""]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, (sid, title, blurb) in enumerate(FEATURED, 1):
            s = by[sid]
            z.write(config.SAMPLES_DIR / s["file"], f"{i}-{s['file']}")
            notes.append(f"{i}. {s['file']}: {title} [{s['expected_decision'].replace('_', ' ').title()}]\n   {blurb}")
        notes += ["", "Start from a clean state (Reset demo data) for the results above, because earlier invoices change PO balances."]
        z.writestr("README.txt", "\n".join(notes))
    from fastapi.responses import Response
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="invoiceflow-samples.zip"'})


@app.get("/api/samples/{sample_id}/pdf")
def sample_pdf(sample_id: str, download: int = 0):
    s = next((x for x in manifest() if x["id"] == sample_id), None)
    if not s:
        raise HTTPException(404, "Unknown sample")
    return FileResponse(config.SAMPLES_DIR / s["file"], media_type="application/pdf", filename=s["file"],
                        content_disposition_type="attachment" if download else "inline")


@app.post("/api/runs/sample/{sample_id}")
def run_sample(sample_id: str):
    s = next((x for x in manifest() if x["id"] == sample_id), None)
    if not s:
        raise HTTPException(404, "Unknown sample")
    data = (config.SAMPLES_DIR / s["file"]).read_bytes()
    run_id = pipeline.new_run(s["file"], "sample", sample_id=sample_id, pdf_bytes=data)
    spawn(run_id, pdf_bytes=data)
    return dict(run_id=run_id)


@app.get("/api/runs")
def runs(limit: int = 200):
    return db.list_runs(limit)


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    r["events"] = db.events_since(run_id, 0)
    sample = next((s for s in manifest() if s["id"] == r.get("sample_id")), None)
    r["expected_decision"] = sample["expected_decision"] if sample else None
    return r


@app.get("/api/runs/{run_id}/pdf")
def run_pdf(run_id: str):
    r = db.get_run(run_id)
    if not r or not r.get("file_path") or not Path(r["file_path"]).exists():
        raise HTTPException(404, "No file stored for this run")
    return FileResponse(r["file_path"], media_type="application/pdf", filename=r["filename"])


@app.get("/api/runs/{run_id}/page/{n}.png")
def run_page(run_id: str, n: int):
    """The invoice page as an image, so the UI can draw evidence boxes on top of it."""
    import io

    import pypdfium2 as pdfium
    from fastapi.responses import Response
    r = db.get_run(run_id)
    if not r or not r.get("file_path") or not Path(r["file_path"]).exists():
        raise HTTPException(404, "No file stored for this run")
    try:
        doc = pdfium.PdfDocument(r["file_path"])
        if n < 0 or n >= len(doc):
            raise HTTPException(404, "No such page")
        buf = io.BytesIO()
        doc[n].render(scale=1.6).to_pil().convert("RGB").save(buf, "PNG")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        raise HTTPException(422, "Could not render this page")
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


@app.get("/api/runs/{run_id}/stream")
async def stream(run_id: str, request: Request):
    if not await run_in_threadpool(db.get_run, run_id):
        raise HTTPException(404, "Run not found")
    last = int(request.headers.get("last-event-id") or request.query_params.get("after") or 0)

    async def gen():
        nonlocal last
        idle_after_done = 0
        while True:
            evs = await run_in_threadpool(db.events_since, run_id, last)
            for e in evs:
                last = e["id"]
                yield dict(id=str(e["id"]), event=e["type"], data=json.dumps({**e["payload"], "stage": e["stage"], "ts": e["ts"]}))
                if e["type"] == "done":
                    return
            status = (await run_in_threadpool(db.one, "SELECT status FROM runs WHERE id=?", (run_id,)) or {}).get("status")
            if status != "running":
                idle_after_done += 1
                if idle_after_done > 3:
                    return
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.1)

    return EventSourceResponse(gen())


class ReviewIn(BaseModel):
    action: str  # approve | reject
    note: Optional[str] = ""
    reviewer: Optional[str] = "AP reviewer"


@app.post("/api/runs/{run_id}/review")
def review(run_id: str, body: ReviewIn):
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    if r["decision"] not in ("HOLD", "ESCALATE") or r["final_decision"]:
        raise HTTPException(409, "Only an open HOLD or ESCALATE can be reviewed")
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    final = "APPROVED" if body.action == "approve" else "REJECTED"
    db.update_run(run_id, final_decision=final, reviewer_note=f"{body.reviewer}: {body.note or '(no note)'}")
    db.set_ledger_status(run_id, "committed" if final == "APPROVED" else "void")
    db.add_event(run_id, "review", None, dict(final_decision=final, reviewer=body.reviewer, note=body.note))
    return db.get_run(run_id)


class ReprocessIn(BaseModel):
    corrections: dict


@app.post("/api/runs/{run_id}/reprocess")
def reprocess(run_id: str, body: ReprocessIn):
    r = db.get_run(run_id)
    if not r or not r.get("result"):
        raise HTTPException(404, "Run not found or has no extraction to correct")
    if r["final_decision"]:
        raise HTTPException(409, "This run has already been reviewed")
    child = pipeline.new_run(r["filename"], "reprocess", sample_id=r.get("sample_id"), parent=run_id)
    spawn(child, prior_extraction=r["result"]["extraction"], corrections=body.corrections)
    return dict(run_id=child)


# --------------------------------------------------------------------------- dashboard
# --------------------------------------------------------------------------- AI help on a finished run
def _finished(run_id: str) -> dict:
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    if not r.get("result"):
        raise HTTPException(409, "This run has not finished yet.")
    return r


@app.get("/api/runs/{run_id}/assist")
def run_assist(run_id: str):
    r = _finished(run_id)
    out = assist.explain(run_id)
    em = (r["result"] or {}).get("email") or {}
    to = em.get("to") if assist.valid_email(em.get("to", "")) else ""
    return dict(out, email_to=to, email_subject=em.get("subject"), internal=bool(em.get("internal")),
                docs=assist.list_docs(run_id), ai=extract_mod.llm_provider() is not None)


class AskIn(BaseModel):
    question: str


@app.post("/api/runs/{run_id}/ask")
def run_ask(run_id: str, body: AskIn):
    _finished(run_id)
    if len(body.question.strip()) < 3:
        raise HTTPException(400, "Type a question.")
    return assist.ask(run_id, body.question)


@app.post("/api/runs/{run_id}/docs")
async def run_add_doc(run_id: str, file: UploadFile = File(...)):
    _finished(run_id)
    data = await file.read()
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(400, "Choose a file under 10 MB.")
    try:
        return assist.add_doc(run_id, file.filename or "document", data)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/stats")
def stats():
    rs = db.list_runs(1000)
    done = [r for r in rs if r["status"] == "done"]
    eff = lambda r: r["effective_decision"]  # noqa: E731
    by = Counter(eff(r) for r in done)
    auto = [r for r in done if r["decision"] in ("APPROVED", "APPROVED_WITH_NOTE") and not r["final_decision"]]
    paid = sum(r["total"] or 0 for r in done if eff(r) in ("APPROVED", "APPROVED_WITH_NOTE"))
    blocked = sum(r["total"] or 0 for r in done if eff(r) in ("HOLD", "REJECTED", "ESCALATE") and r["decision"] != "APPROVED")
    reasons = Counter(c for r in done for c in r["reason_codes"])
    proc = [r["duration_ms"] for r in done if r["duration_ms"]]
    today = datetime.now().date()
    days = []
    for i in range(13, -1, -1):
        dd = today - timedelta(days=i)
        row = dict(date=dd.isoformat(), APPROVED=0, HOLD=0, REJECTED=0)
        for r in done:
            if datetime.fromtimestamp(r["created_at"]).date() == dd:
                e = eff(r)
                key = "APPROVED" if e in ("APPROVED", "APPROVED_WITH_NOTE") else "HOLD" if e == "HOLD" else "REJECTED"
                row[key] += 1
        days.append(row)
    queue = [r for r in done if r["decision"] in ("HOLD", "ESCALATE") and not r["final_decision"]]
    outcomes = []
    for k in ("APPROVED", "APPROVED_WITH_NOTE", "HOLD", "ESCALATE", "REJECTED"):
        grp = [r for r in done if eff(r) == k]
        if grp:
            outcomes.append(dict(outcome=k, count=len(grp), amount=round(sum(r["total"] or 0 for r in grp), 2)))
    return dict(
        total=len(done), by_decision=dict(by), stp_rate=round(len(auto) / len(done), 3) if done else 0,
        amount_approved=round(paid, 2), amount_blocked=round(blocked, 2),
        avg_processing_ms=int(sum(proc) / len(proc)) if proc else 0,
        by_outcome=outcomes,
        top_reasons=[dict(code=k, count=v) for k, v in reasons.most_common(8)], by_day=days,
        review_queue=[dict(id=r["id"], invoice_number=r["invoice_number"], vendor_name=r["vendor_name"], total=r["total"],
                           decision=r["decision"], codes=r["reason_codes"]) for r in queue[:100]],
        running=sum(1 for r in rs if r["status"] == "running"))


@app.get("/api/purchase-orders")
def purchase_orders():
    m = master.load()
    added = {r["number"] for r in db.custom_pos()}
    out = []
    for po in m.pos.values():
        committed = sum(r["amount"] for r in db.ledger_for_po(po.number, ("committed",)))
        pending = sum(r["amount"] for r in db.ledger_for_po(po.number, ("pending",)))
        v = m.vendors[po.vendor_id]
        out.append(dict(po_number=po.number, vendor=v.name, vendor_tax_id=v.tax_id, description=po.description, total=po.total,
                        billed=round(committed, 2), pending=round(pending, 2), remaining=round(po.total - committed, 2),
                        pct=round(committed / po.total, 3) if po.total else 0, invoices=len(db.ledger_for_po(po.number, ("committed",))),
                        expiry=str(po.expiry_date), status=po.status, added=po.number in added))
    out.sort(key=lambda p: (not p["added"], p["po_number"]))
    return out


@app.get("/api/vendors")
def vendors():
    return [dict(id=v.id, name=v.name, tax_id=v.tax_id, bank_last4=v.bank_last4, status=v.status) for v in master.load().vendors.values()]


class VendorIn(BaseModel):
    name: str
    tax_id: str
    bank_last4: str
    email: Optional[str] = ""


class POIn(BaseModel):
    po_number: str
    vendor_id: str
    description: str
    total: float
    expiry_date: str
    issue_date: Optional[str] = None


@app.post("/api/vendors")
def add_vendor(v: VendorIn):
    m = master.load()
    name, tax, bank = v.name.strip(), v.tax_id.strip(), v.bank_last4.strip()
    if len(name) < 2:
        raise HTTPException(400, "Enter the vendor's legal name.")
    if not re.fullmatch(r"\d{2}-\d{7}", tax):
        raise HTTPException(400, "Tax ID must look like 12-3456789.")
    if not re.fullmatch(r"\d{4}", bank):
        raise HTTPException(400, "Bank account: enter the last 4 digits.")
    if m.vendor_by_tax(tax):
        raise HTTPException(409, f"Tax ID {tax} already belongs to {m.vendor_by_tax(tax).name}.")
    if any(master.norm_name(x.name) == master.norm_name(name) for x in m.vendors.values()):
        raise HTTPException(409, "A vendor with that name already exists.")
    vid = f"U{len(db.custom_vendors()) + 1:03d}"
    db.add_custom_vendor(vid, name, tax, bank, (v.email or "").strip())
    return dict(id=vid, name=name, tax_id=tax)


@app.post("/api/purchase-orders")
def add_po(p: POIn):
    m = master.load()
    num = re.sub(r"\s", "", p.po_number.upper())
    m_ = re.fullmatch(r"(?:PO-?)?(\d{3,})", num)
    if m_:
        num = "PO-" + m_[1]
    if not re.fullmatch(r"PO-\d{3,}", num):
        raise HTTPException(400, "PO number must look like PO-4601 (PO, a dash, then at least 3 digits) so it can be read off an invoice.")
    if num in m.pos:
        raise HTTPException(409, f"{num} already exists.")
    if p.vendor_id not in m.vendors:
        raise HTTPException(400, "Choose a vendor.")
    if m.vendors[p.vendor_id].status != "approved":
        raise HTTPException(400, f"{m.vendors[p.vendor_id].name} is not an approved vendor.")
    if not p.total or p.total <= 0:
        raise HTTPException(400, "Enter the PO value, more than zero.")
    try:
        issue = date.fromisoformat(p.issue_date) if p.issue_date else config.today()
        expiry = date.fromisoformat(p.expiry_date)
    except ValueError:
        raise HTTPException(400, "Enter valid dates.")
    if expiry < issue:
        raise HTTPException(400, "The expiry date is before the issue date.")
    db.add_custom_po(num, p.vendor_id, (p.description or "").strip() or "Purchase order", "USD", round(p.total, 2), issue.isoformat(), expiry.isoformat())
    return dict(po_number=num)


@app.delete("/api/purchase-orders/{po_number}")
def delete_po(po_number: str):
    """Delete a PO that was added in the app. The built-in POs cannot be deleted."""
    if po_number in {r["number"] for r in db.custom_pos()}:
        db.delete_custom_po(po_number)
        return dict(deleted=po_number)
    if po_number in master.load().pos:
        raise HTTPException(403, "Built-in purchase orders cannot be deleted. Only ones you added can.")
    raise HTTPException(404, "Unknown PO")


class TestInvoiceIn(BaseModel):
    amount: Optional[float] = None


def _po_invoice_pdf(po_number: str, amount: Optional[float]) -> tuple[str, bytes]:
    """Make a PDF invoice for this PO, from its vendor. Defaults to the amount still left on the PO."""
    import random
    import tempfile

    from scripts import gen_data
    m = master.load()
    po = m.pos.get(po_number)
    if not po:
        raise HTTPException(404, "Unknown PO")
    v = m.vendors[po.vendor_id]
    committed = sum(r["amount"] for r in db.ledger_for_po(po.number))
    amt = round(amount if amount else max(po.total - committed, 0) or po.total, 2)
    if amt <= 0 or amt > 1_000_000_000:
        raise HTTPException(400, "Enter an amount above zero.")
    tag = "".join(ch for ch in v.name.upper() if ch.isalpha())[:2] or "XX"
    inv_no = f"INV-{tag}-{random.randint(10000, 99999)}"
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / f"{inv_no}.pdf"
        gen_data.build_invoice(path, vendor_override=dict(vendor_name=v.name, tax_id=v.tax_id, bank_last4=v.bank_last4,
                                                          address=["Remit address on file", "United States"]),
                               template="A", inv_no=inv_no, inv_date=config.today(), po_ref=po.number,
                               lines=[(po.description or "Goods and services", 1, amt, None)])
        return inv_no, path.read_bytes()


@app.post("/api/purchase-orders/{po_number}/test-invoice")
def test_invoice(po_number: str, body: TestInvoiceIn = TestInvoiceIn()):
    """Make an invoice for this PO and run it, so a new PO can be tried straight away."""
    inv_no, data = _po_invoice_pdf(po_number, body.amount)
    run_id = pipeline.new_run(f"{inv_no}.pdf", "upload", pdf_bytes=data)
    spawn(run_id, pdf_bytes=data)
    return dict(run_id=run_id)


@app.get("/api/purchase-orders/{po_number}/invoice.pdf")
def download_po_invoice(po_number: str, amount: Optional[float] = None):
    """Download an invoice for this PO without running it, to upload by hand."""
    inv_no, data = _po_invoice_pdf(po_number, amount)
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{inv_no}.pdf"'})


@app.get("/api/eval")
def get_eval():
    """Reader accuracy report (scripts/eval_extraction.py). Built in the background on first start."""
    f = config.DATA_DIR / "eval.json"
    if not f.exists():
        return JSONResponse(dict(ready=False), status_code=202)
    return dict(ready=True, **json.loads(f.read_text()))


def _build_eval_if_missing():
    if (config.DATA_DIR / "eval.json").exists():
        return
    try:
        from scripts import eval_extraction
        eval_extraction.main()
    except Exception:  # noqa: BLE001 - a missing report must never stop the app
        pass


@app.get("/api/config")
def get_config():
    return dict(auto_tolerance_pct=config.AUTO_TOLERANCE_PCT, auto_tolerance_abs=config.AUTO_TOLERANCE_ABS,
                review_tolerance_pct=config.REVIEW_TOLERANCE_PCT, dup_window_days=config.DUP_WINDOW_DAYS,
                min_field_confidence=config.MIN_FIELD_CONFIDENCE, implied_po_conf=config.IMPLIED_PO_APPROVE_CONF,
                implied_po_margin=config.IMPLIED_PO_MIN_MARGIN, math_tolerance=config.MATH_TOLERANCE_ABS,
                line_price_tolerance_pct=config.LINE_PRICE_TOLERANCE_PCT, name_variant=config.NAME_VARIANT_THRESHOLD)


@app.post("/api/admin/reset")
def reset(request: Request):
    # Wipes every run. Only callable from the machine itself (or the test client), never from the public link.
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "testclient"):
        raise HTTPException(403, "Reset is only available from the server itself")
    ensure_ready(force_seed=True)
    return dict(ok=True)


def _ai_status():
    p = extract_mod.llm_provider()
    return dict(provider=p or "off", model=extract_mod.llm_model())


@app.get("/api/settings")
def get_settings():
    return _ai_status()


@app.get("/api/health")
def health():
    return dict(ok=True, time=time.time(), llm=extract_mod.llm_provider() or "off", model=extract_mod.llm_model(), version=APP_VERSION)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
