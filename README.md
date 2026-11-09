---
title: InvoiceFlow
emoji: 🧾
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# InvoiceFlow: PS-1, invoice PDF to decision

A working invoice-processing pipeline with a live run view, a dashboard of real runs, and a purchase-order register you can add to.
Upload a PDF (or click a sample) and watch it move through seven stages:
receive, read, validate, verify vendor, scan duplicates, match to PO, decide.

## Run it

    pip install -r requirements.txt     # plus: apt install tesseract-ocr
    uvicorn app.main:app --port 8000    # open http://localhost:8000
    pytest tests -q                     # 43 tests: scenarios, evidence boxes, degraded scans, added POs, AI reader
    python -m scripts.eval_extraction   # field-level accuracy report (also shown on the How it works page)

Sample invoices and the procurement register (xlsx) are generated on first start. The dashboard starts empty and only ever shows invoices you ran. The one piece of starting data is an opening PO ledger (invoices billed before this tool), which the split-PO examples rely on.
Add a PO: Purchase orders page, "Add a purchase order" (a new vendor can be added in the same form). Invoices match it on the PO number printed on them and the vendor's tax ID; "Try invoice" makes and runs one for you. "Reset demo data" clears runs and balances but keeps POs and vendors you added.
Sample invoices (one clean, four edge cases) are in the top-right menu, each with Run, View and Download, plus a zip of all five.
Optional AI second reader: put GROQ_API_KEY=... in a `.env` file in this folder (see `.env.example`), or export it. Also supported: ANTHROPIC_API_KEY (model via IF_LLM_MODEL, default openai/gpt-oss-120b) or ANTHROPIC_API_KEY. It can fill in a field the rules could not read, but only with text that is written on the page, and it lowers confidence where the two readers disagree. It never approves or rejects.

Upload a PDF or a photo (JPG, PNG, WebP); photos are turned into a one page PDF and read like a scan. A card payment receipt (approval code, card type, transaction ID and similar, with no invoice wording) is refused as "Not an invoice", because it was already paid by card.

Scanned invoices are read with Tesseract OCR first. Only when a scan is hard to read (average OCR confidence under 85%, or a key field missing or unsure) is the page image also sent to a vision model (default `qwen/qwen3.8-27b` on Groq, change with `IF_VISION_MODEL`, turn off with `IF_VISION=off`). Values it agrees on gain confidence; a value it supplies is trusted only if the page backs it up (the numbers add up, or it closely matches what OCR saw), otherwise the invoice goes to a person with the vision reading pre-filled. It never decides anything, and page images are sent to the provider only in this fallback case.

On a finished invoice the same AI also writes a two-line plain-English summary, drafts the follow-up email from the checks' facts, and answers questions in an "Ask about this invoice" box. Answers come only from this invoice's own documents (the PDF, the checks, the vendor and PO records, other invoices on the PO, and any document you add), with the sources shown. The email opens in the user's own mail app, ready to send.
Docker: `docker build -t invoiceflow . && docker run -p 7860:7860 invoiceflow`.

## Design in one paragraph
A model-free reader (PDF text layer, OCR fallback) gives every field a confidence score. A deterministic
rules engine makes the decision, and every check records its evidence. The worst finding wins on the ladder
Approved, Approved with note, Hold, Rejected, Escalate. An LLM may help read a document but never approves or rejects.

## Edge cases that are deliberately deep
1. Split POs: cumulative billing across invoices, so each invoice looks fine alone but the total breaches the PO.
2. Missing PO number: infer it from vendor, amount and line items, with a confidence and a margin over the runner-up.
3. Duplicates: exact, and re-issued under a new number (same vendor, amount, PO and lines within 14 days).
4. Lookalike vendor with a changed bank account: everything matches, the bank does not, so escalate and alert internally only.
Plus: scanned image with skew and noise, arithmetic mismatch, unknown vendor, missing fields, tolerance bands (2% or 500 auto, 2-10% review, above 10% reject).

## Evidence and measured accuracy
- **Show on invoice:** every value the reader found has a box on the page. Click a row in "What was read" and the invoice opens with that value highlighted (digital PDFs and OCR scans).
- **Accuracy report:** 100% of fields correct on the generated invoices, 96.1% when every invoice is re-scanned with noise, skew and lower resolution. Confidence is lower on the wrong fields (0.62) than the right ones (0.87).
- **Safety under noise:** a test runs all 12 degraded scans through the full process and asserts no invoice that should not be paid ever gets approved. Noise can make it more cautious (2 of 12 became holds), never less.
- The invoices are synthetic, so the first figure is not a claim about invoices in the wild.

## Human in the loop
Held items can be approved or rejected with an audit note, or fixed and re-run with corrected fields.

## Suggested live demo order (5 minutes)
1. Clean match (live stages; then click "Show on invoice" and a row to see where a value was read).
2. Scanned image, no PO (OCR plus PO inference).
3. Split PO pushes over (cumulative ledger).
4. Near-duplicate (same bill, new number).
5. Lookalike bank (escalation, internal-only alert).
6. Missing fields: use "fix and re-run", then the Dashboard.
Use "Reset demo data" in the sidebar before you start.


## Deploy to Google Cloud Run

Run from this folder on your own computer, after `gcloud auth login`:

    PROJECT_ID=your-project-id bash deploy/gcp.sh

The script stores the Groq key and a shared password in Secret Manager, then deploys one always-on instance in Mumbai (set `REGION` to change it). Reviewers sign in with any username and the shared password (`APP_PASSWORD`). `/api/health` stays open for health checks. Data is kept in the instance, so it resets on a restart and the app reseeds itself. At list prices, one 1 vCPU / 1 GiB instance kept warm costs roughly $12 a month before any free credits. Set a billing budget alert before sharing the link.
