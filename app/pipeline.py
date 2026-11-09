"""Orchestrates the stages and streams every step into the event log (which the UI tails over SSE)."""
from __future__ import annotations

import hashlib
import time
import traceback
import uuid
from typing import Optional

from . import config, db, master, rules
from .extract import Extraction, FieldValue, detect_receipt, extract, norm_invoice_no

STAGES = [
    ("ingest", "Receive and inspect the file"),
    ("extract", "Read the invoice"),
    ("validate", "Validate fields and arithmetic"),
    ("vendor", "Verify vendor and bank details"),
    ("duplicates", "Scan for duplicates"),
    ("match", "Match to purchase order"),
    ("decide", "Decide"),
]
NUMERIC = {"total", "subtotal", "tax", "tax_rate"}


class Emitter:
    def __init__(self, run_id: str, delay: float):
        self.run_id, self.delay, self.slept = run_id, delay, 0.0

    def _pause(self, mult=1.0):
        if self.delay > 0:
            time.sleep(self.delay * mult)
            self.slept += self.delay * mult

    def event(self, type_, stage, payload, pause=0.0):
        db.add_event(self.run_id, type_, stage, payload)
        if pause:
            self._pause(pause)

    def stage_start(self, stage):
        title = dict(STAGES)[stage]
        self.event("stage_start", stage, dict(title=title), pause=1.0)

    def check(self, c: rules.Check):
        self.event("check", c.stage, c.to_dict())

    def stage_done(self, stage, checks, **extra):
        worst = max((rules.RANK[c.outcome] for c in checks), default=0)
        outcome = ["pass", "warn", "hold", "reject", "escalate"][worst]
        self.event("stage_done", stage, dict(outcome=outcome, **extra), pause=1.0)


def extraction_from_dict(d: dict) -> Extraction:
    ex = Extraction(method=d["method"], ocr_confidence=d.get("ocr_confidence"), page_count=d.get("page_count", 1), text="",
                    lines=d.get("lines", []), llm=d.get("llm", {}))
    ex.fields = {k: FieldValue(v["value"], v["confidence"], v.get("source", ""), v.get("note", ""), v.get("box")) for k, v in d["fields"].items()}
    return ex


def apply_corrections(ex: Extraction, corrections: dict) -> list[str]:
    changed = []
    for k, v in corrections.items():
        if v in (None, ""):
            continue
        if k in NUMERIC:
            v = float(str(v).replace(",", ""))
        ex.fields[k] = FieldValue(v, 1.0, "human correction", "entered by a reviewer")
        changed.append(k)
    return changed


def new_run(filename, source, sample_id=None, parent=None, pdf_bytes: Optional[bytes] = None) -> str:
    run_id = uuid.uuid4().hex[:10]
    fh, fp = None, None
    if pdf_bytes is not None:
        fh = hashlib.sha256(pdf_bytes).hexdigest()
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        fp = str(config.UPLOAD_DIR / f"{run_id}.pdf")
        open(fp, "wb").write(pdf_bytes)
    db.create_run(run_id, filename, source, sample_id, parent, fh, fp)
    return run_id


def execute_run(run_id: str, pdf_bytes: Optional[bytes] = None, prior_extraction: Optional[dict] = None,
                corrections: Optional[dict] = None, delay: Optional[float] = None):
    """Runs all stages. Never raises: failures become an explicit, explained HOLD."""
    em = Emitter(run_id, config.STAGE_DELAY if delay is None else delay)
    t0 = time.time()
    checks: list[rules.Check] = []
    ctx = None
    try:
        mst = master.load()
        run = db.get_run(run_id)
        if run["parent_run_id"]:
            # A corrected re-run supersedes the held invoice; otherwise it would be flagged as a duplicate of itself.
            db.set_ledger_status(run["parent_run_id"], "void")

        # ---------------- ingest
        em.stage_start("ingest")
        stage = []
        if pdf_bytes is not None:
            sha = run["file_hash"]
            stage.append(rules.chk("ingest", "FILE_RECEIVED", "File received", "pass",
                                   f"{run['filename']}, {len(pdf_bytes) / 1024:.0f} KB, sha256 {sha[:12]}...", size=len(pdf_bytes), sha256=sha))
            if run["filename"].lower().endswith((".jpg", ".jpeg", ".png", ".webp")) and pdf_bytes.startswith(b"%PDF"):
                stage.append(rules.chk("ingest", "PHOTO_CONVERTED", "Photo received", "info", "This is a photo. It was turned into a one page PDF so it can be read like a scan."))
            if not pdf_bytes.startswith(b"%PDF"):
                stage.append(rules.chk("ingest", "UNREADABLE_DOCUMENT", "File type", "hold", "This is not a PDF file."))
            seen = db.rows("SELECT id, decision FROM runs WHERE file_hash=? AND id!=? AND source!='opening' AND status='done'", (sha, run_id))
            if seen:
                stage.append(rules.chk("ingest", "FILE_SEEN_BEFORE", "Repeat file", "info",
                                       f"A byte-identical file was already processed in run {seen[0]['id']} ({seen[0]['decision']}).", run=seen[0]["id"]))
        else:
            stage.append(rules.chk("ingest", "REPROCESS", "Re-run with corrections", "info",
                                   "Re-using the document already read; a reviewer has corrected some fields.", parent=run["parent_run_id"]))
        for c in stage:
            em.check(c)
        checks += stage
        em.stage_done("ingest", stage)
        unreadable = any(c.code == "UNREADABLE_DOCUMENT" for c in stage)

        # ---------------- extract
        em.stage_start("extract")
        stage, ex = [], None
        if prior_extraction is not None:
            ex = extraction_from_dict(prior_extraction)
            changed = apply_corrections(ex, corrections or {})
            stage.append(rules.chk("extract", "HUMAN_CORRECTION", "Reviewer corrections", "info",
                                   "Applied corrections to: " + (", ".join(changed) or "nothing") + ".", fields=changed))
        elif not unreadable:
            try:
                ex = extract(pdf_bytes)
                if len(ex.text.strip()) < 20:
                    raise ValueError("no text could be read")
            except Exception as e:  # noqa: BLE001
                ex = None
                stage.append(rules.chk("extract", "UNREADABLE_DOCUMENT", "Readable document", "hold",
                                       f"Could not read this file ({str(e)[:80]}). It may be corrupt, encrypted or a blank scan."))
        if ex is not None:
            if ex.method == "digital":
                stage.append(rules.chk("extract", "EXTRACT_DIGITAL", "Extraction method", "info",
                                       "Text layer found in the PDF, so it was read directly (no OCR needed).", method="digital", pages=ex.page_count))
            elif ex.method == "ocr":
                stage.append(rules.chk("extract", "EXTRACT_OCR", "Extraction method", "info",
                                       f"No text layer: this is a scanned image. Read with OCR at {ex.ocr_confidence:.0%} average character confidence.",
                                       method="ocr", ocr_confidence=ex.ocr_confidence))
            receipt = detect_receipt(ex.text)
            if receipt:
                stage.append(rules.chk("extract", "RECEIPT_NOT_INVOICE", "Document type", "reject",
                                       f"This is a card payment receipt (found {', '.join(receipt[:4])}). It was already paid by card, so it is not an invoice.", markers=receipt))
            v = ex.vision or {}
            nice = lambda names: ", ".join(n.replace("_", " ") for n in names)
            if v.get("status") == "ok":
                parts = [f"The scan was hard to read ({v['trigger']}), so a vision model ({v['model']}) also read the page image."]
                if v["confirmed"]:
                    parts.append(f"It agreed with OCR on {nice(v['confirmed'])}.")
                if v["filled"]:
                    parts.append(f"It filled in {nice(v['filled'])}.")
                if v["unconfirmed"]:
                    parts.append(f"Nothing on the page confirms {nice(v['unconfirmed'])}, so a person must check it.")
                if v["conflicts"]:
                    parts.append(f"It disagreed with OCR on {nice(v['conflicts'])}, which lowers confidence.")
                stage.append(rules.chk("extract", "VISION_READ", "Vision reader", "info", " ".join(parts), **v))
            elif v.get("status") == "error":
                stage.append(rules.chk("extract", "VISION_READ", "Vision reader", "info",
                                       f"The scan was hard to read ({v['trigger']}). The vision reader could not run: {v['reason']}. The OCR reading was used.", **v))
            if ex.llm.get("status") == "skipped" and ex.llm.get("reason") != "disabled":
                stage.append(rules.chk("extract", "LLM_CROSSCHECK", "AI second reader", "info", "Off: no GROQ_API_KEY is set, so only the rules reader was used.", **ex.llm))
            elif ex.llm.get("status") == "error":
                stage.append(rules.chk("extract", "LLM_CROSSCHECK", "AI second reader", "info", f"Could not run: {ex.llm.get('reason', 'unknown error')}. The rules reader alone was used.", **ex.llm))
            if ex.llm.get("status") == "ok":
                dis, fil = ex.llm.get("disagree", {}), ex.llm.get("filled", [])
                msg = f"A second reader (AI) agreed on {len(ex.llm['agree'])} field(s)"
                if fil:
                    msg += f", filled in {', '.join(f.replace('_', ' ') for f in fil)} that the rules could not read (taken from the page)"
                if dis:
                    msg += f", and disagreed on {', '.join(d.replace('_', ' ') for d in dis)}"
                stage.append(rules.chk("extract", "LLM_CROSSCHECK", "AI second reader", "warn" if (dis or fil) else "info", msg + ".", **ex.llm))
        for c in stage:
            em.check(c)
        checks += stage
        if ex is not None:
            em.event("artifact", "extract", dict(name="extraction", data=ex.to_dict()))
        em.stage_done("extract", stage)

        if ex is None:
            ex = Extraction(method="none", ocr_confidence=None, page_count=0, text="")
        ctx = rules.Ctx(ex=ex, master=mst)

        # ---------------- rules stages
        fns = [("validate", rules.validate), ("vendor", rules.check_vendor), ("duplicates", rules.check_duplicates), ("match", rules.check_match)]
        for name, fn in fns:
            em.stage_start(name)
            stop = next((c.code for c in checks if c.code in ("UNREADABLE_DOCUMENT", "RECEIPT_NOT_INVOICE")), None)
            if stop:
                skip = [rules.chk(name, "SKIPPED", "Skipped", "info", "Skipped: this is a receipt, not an invoice." if stop == "RECEIPT_NOT_INVOICE" else "Skipped: the document could not be read.")]
                for c in skip:
                    em.check(c)
                em.stage_done(name, skip, skipped=True)
                continue
            stage = fn(ctx)
            for c in stage:
                em.check(c)
            checks += stage
            em.stage_done(name, stage)

        # ---------------- decide
        em.stage_start("decide")
        result = rules.decide(ctx, checks)
        result["checks"] = [c.to_dict() for c in checks]
        result["extraction"] = ex.to_dict()
        if any(c.code == "UNREADABLE_DOCUMENT" for c in checks):
            result["actions"].append(dict(code="UNREADABLE_DOCUMENT", owner="AP", action=rules.ACTIONS["UNREADABLE_DOCUMENT"][1]))
        em.event("decision", "decide", {k: v for k, v in result.items() if k not in ("checks", "extraction")}, pause=0.5)

        # ---------------- ledger
        status = {"APPROVED": "committed", "APPROVED_WITH_NOTE": "committed", "HOLD": "pending"}.get(result["decision"])
        total = ex.get("total")
        if status and ctx.vendor and total is not None:
            db.add_ledger(ctx.po.number if ctx.po else None, ctx.vendor.id, ex.get("invoice_number"), norm_invoice_no(ex.get("invoice_number")),
                          total, ex.get("invoice_date"), " ".join(i["description"] for i in ex.lines), status, run_id)

        em.stage_done("decide", [], outcome_override=result["decision"])
        duration = int((time.time() - t0 - em.slept) * 1000)
        db.update_run(run_id, status="done", finished_at=time.time(), decision=result["decision"],
                      vendor_name=(ctx.vendor.name if ctx.vendor else ex.get("vendor_name")), invoice_number=ex.get("invoice_number"),
                      po_number=ctx.po.number if ctx.po else ex.get("po_number"), total=total, currency=ex.get("currency", "USD"),
                      reason_codes=__import__("json").dumps(result["reason_codes"]), summary=result["explanation"],
                      duration_ms=duration, result_json=__import__("json").dumps(result, default=str))
        em.event("done", None, dict(decision=result["decision"], processing_ms=duration))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        db.add_event(run_id, "error", None, dict(message=f"Unexpected error: {e}"))
        db.update_run(run_id, status="error", finished_at=time.time(), decision="HOLD",
                      summary=f"Processing failed unexpectedly ({e}). Nothing was approved.")
        db.add_event(run_id, "done", None, dict(decision="HOLD", processing_ms=int((time.time() - t0) * 1000)))


def run_sync(**kw):
    """Convenience for tests and scripts."""
    rid = new_run(kw["filename"], kw.get("source", "test"), kw.get("sample_id"), kw.get("parent"), kw.get("pdf_bytes"))
    execute_run(rid, pdf_bytes=kw.get("pdf_bytes"), prior_extraction=kw.get("prior_extraction"), corrections=kw.get("corrections"), delay=0)
    return db.get_run(rid)
