"""Generate the demo world: procurement workbook (vendors, POs, PO lines), invoice PDFs and a manifest.

All dates are relative to today so the demo never goes stale (POs never expire under your feet).
Run:  python -m scripts.gen_data
"""
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from openpyxl import Workbook
from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402

TODAY = config.today()
BUYER = "Larkspur Industries Inc."


def d(offset: int) -> date:
    return TODAY + timedelta(days=offset)


def money(x: float) -> str:
    return f"{x:,.2f}"


# --------------------------------------------------------------------------- master data
VENDORS = [
    # id, name, tax id, bank last4, status, email, address lines
    ("V001", "Northwind Industrial Supply LLC", "84-1920374", "4821", "approved", "ar@northwind-supply.example", ["1200 Foundry Road", "Pittsburgh, PA 15201"]),
    ("V002", "Brightpath Logistics Inc", "52-7730418", "7730", "approved", "billing@brightpath-log.example", ["88 Harbor Way", "Savannah, GA 31401"]),
    ("V003", "Helix Office Solutions Ltd", "36-5521096", "1156", "approved", "accounts@helix-office.example", ["400 Market Street", "Chicago, IL 60606"]),
    ("V004", "Orion Cloud Services Inc", "91-4408852", "9902", "approved", "invoices@orion-cloud.example", ["2100 Mission Street", "San Francisco, CA 94110"]),
    ("V005", "Meridian Packaging Co", "47-6613209", "3347", "approved", "ar@meridian-pack.example", ["15 Mill Lane", "Dayton, OH 45402"]),
    ("V006", "Zenith Traders LLC", "20-3318841", "6620", "blocked", "sales@zenith-traders.example", ["9 Quay Street", "Newark, NJ 07102"]),
]
V = {v[0]: v for v in VENDORS}

# po, vendor, description, tax_rate, lines[(desc, qty, unit)]
POS = [
    ("PO-4501", "V001", "Industrial fasteners and fittings", 0.08,
     [("Hex bolts M10", 500, 2.40), ("Steel washers M10", 1000, 0.35), ("Pipe fittings 1 inch", 120, 12.50)]),
    ("PO-4502", "V002", "Freight services Q4", 0.0,
     [("Freight and logistics services Q4", 1, 12000.00)]),
    ("PO-4503", "V003", "Office furniture, phased delivery", 0.0,
     [("Office furniture phased delivery", 1, 30000.00)]),
    ("PO-4504", "V004", "Annual cloud platform subscription", 0.0,
     [("Annual platform subscription", 1, 24000.00)]),
    ("PO-4505", "V005", "Packaging materials", 0.05,
     [("Corrugated boxes 24x18x12", 800, 4.20), ("Bubble wrap roll 500ft", 60, 18.00),
      ("Packing tape 2 inch case", 120, 9.00), ("Pallet stretch wrap", 90, 36.00)]),
    ("PO-4506", "V001", "Safety equipment", 0.08,
     [("Safety gloves", 200, 6.50), ("Hard hats", 50, 18.00)]),
    ("PO-4507", "V003", "Office supplies", 0.10,
     [("Paper reams A4", 100, 4.50), ("Toner cartridges", 10, 85.00), ("Pen boxes", 40, 7.50)]),
    ("PO-4508", "V005", "Warehouse labelling", 0.05,
     [("Shelf labels", 500, 0.80), ("Barcode scanners", 4, 125.00)]),
]


def po_totals(lines, rate):
    sub = round(sum(q * u for _, q, u in lines), 2)
    tax = round(sub * rate, 2)
    return sub, tax, round(sub + tax, 2)


def write_workbook(path: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Vendors"
    ws.append(["vendor_id", "name", "tax_id", "bank_last4", "status", "email"])
    for v in VENDORS:
        ws.append([v[0], v[1], v[2], v[3], v[4], v[5]])
    ws = wb.create_sheet("PurchaseOrders")
    ws.append(["po_number", "vendor_id", "description", "currency", "subtotal", "tax_rate", "tax", "total", "issue_date", "expiry_date", "status"])
    for po, vid, desc, rate, lines in POS:
        sub, tax, tot = po_totals(lines, rate)
        ws.append([po, vid, desc, "USD", sub, rate, tax, tot, d(-60).isoformat(), d(120).isoformat(), "open"])
    ws = wb.create_sheet("POLines")
    ws.append(["po_number", "line_no", "description", "qty", "unit_price", "amount"])
    for po, _, _, _, lines in POS:
        for i, (desc, q, u) in enumerate(lines, 1):
            ws.append([po, i, desc, q, u, round(q * u, 2)])
    wb.save(path)


# --------------------------------------------------------------------------- invoice rendering
def lines_from_po(po_number):
    for po, _, _, rate, lines in POS:
        if po == po_number:
            return [(desc, q, u, round(q * u, 2)) for desc, q, u in lines], rate
    raise KeyError(po_number)


def fmt_date(dt: date, style: str) -> str:
    if style == "iso":
        return dt.isoformat()
    if style == "us":
        return dt.strftime("%b ") + str(dt.day) + dt.strftime(", %Y")
    return f"{dt.day:02d} " + dt.strftime("%b %Y")


def _hdr_vendor(c, inv, x, y, size=16):
    c.setFont("Helvetica-Bold", size)
    c.drawString(x, y, inv["vendor_name"])
    c.setFont("Helvetica", 9)
    yy = y - 14
    for a in inv["address"]:
        c.drawString(x, yy, a)
        yy -= 12
    return yy


def _table(c, inv, y0, heads, xs):
    c.setFont("Helvetica-Bold", 9)
    c.drawString(xs[0], y0, heads[0])
    for x, h in zip(xs[1:], heads[1:]):
        c.drawRightString(x, y0, h)
    c.line(50, y0 - 5, 562, y0 - 5)
    c.setFont("Helvetica", 10)
    y = y0 - 22
    for desc, q, u, amt in inv["lines"]:
        c.drawString(xs[0], y, desc)
        qs = f"{q:g}"
        c.drawRightString(xs[1], y, qs)
        c.drawRightString(xs[2], y, money(u))
        c.drawRightString(xs[3], y, money(amt))
        y -= 18
    c.line(50, y + 8, 562, y + 8)
    return y - 10


def _totals(c, inv, y, labels, xl=380, xr=562):
    c.setFont("Helvetica", 10)
    rows = []
    if not inv.get("embed_tax"):
        rows.append((labels[0], money(inv["subtotal"])))
        rows.append((labels[1].format(pct=f"{inv['tax_rate'] * 100:g}%"), money(inv["tax"])))
    rows.append((labels[2], money(inv["total"])))
    for i, (lab, val) in enumerate(rows):
        last = i == len(rows) - 1
        c.setFont("Helvetica-Bold" if last else "Helvetica", 11 if last else 10)
        c.drawString(xl, y, lab)
        c.drawRightString(xr, y, val)
        y -= 16
    return y


def draw_A(c, inv):
    _hdr_vendor(c, inv, 50, 745, 18)
    c.setFont("Helvetica", 9)
    c.drawString(50, 707, f"Tax ID: {inv['tax_id']}")
    c.setFont("Helvetica-Bold", 22)
    c.setFillColor(colors.HexColor("#555555"))
    c.drawRightString(562, 700, "INVOICE")
    c.setFillColor(colors.black)
    y = 676
    meta = [("Invoice No:", inv.get("inv_no")), ("Invoice Date:", fmt_date(inv["inv_date"], "iso") if inv.get("inv_date") else None),
            ("Due Date:", fmt_date(inv["due_date"], "iso")), ("PO Number:", inv.get("po_ref"))]
    c.setFont("Helvetica", 10)
    for lab, val in meta:
        if val:
            c.drawString(380, y, lab)
            c.drawString(470, y, val)
            y -= 15
    c.drawString(50, 676, "Bill To:")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, 661, BUYER)
    c.setFont("Helvetica", 9)
    c.drawString(50, 649, "Accounts Payable")
    ty = _table(c, inv, 590, ["Description", "Qty", "Unit Price", "Amount (USD)"], [50, 330, 430, 562])
    ty = _totals(c, inv, ty, ["Subtotal", "Sales Tax ({pct})", "Total Due"])
    c.setFont("Helvetica", 9)
    c.drawString(50, 110, f"Remit to: First National Bank | Account: ****{inv['bank_last4']}")
    c.drawString(50, 97, "Payment terms: Net 30")


def draw_B(c, inv):
    c.setFillColor(colors.HexColor("#1f3b5c"))
    c.rect(0, 752, 612, 40, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, 766, "TAX INVOICE")
    c.setFillColor(colors.black)
    _hdr_vendor(c, inv, 50, 722, 15)
    c.setFont("Helvetica", 9)
    c.drawString(50, 680, f"EIN {inv['tax_id']}")
    y = 722
    c.setFont("Helvetica", 10)
    meta = [("Inv #", inv.get("inv_no")), ("Date", fmt_date(inv["inv_date"], "us") if inv.get("inv_date") else None),
            ("Customer PO", inv.get("po_ref")), ("Terms", "Net 30")]
    for lab, val in meta:
        if val:
            c.drawString(390, y, lab)
            c.drawString(470, y, val)
            y -= 15
    c.drawString(50, 655, "Customer:")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(110, 655, BUYER)
    ty = _table(c, inv, 600, ["Item / Description", "Qty", "Rate", "Total"], [50, 340, 440, 562])
    ty = _totals(c, inv, ty, ["Sub-total", "Tax ({pct})", "Amount Due"])
    c.setFont("Helvetica", 9)
    c.drawString(50, 110, f"ACH Acct No. XXXXXX{inv['bank_last4']}")


def draw_C(c, inv):
    _hdr_vendor(c, inv, 50, 745, 16)
    c.setFont("Helvetica", 9)
    c.drawString(50, 705, f"Federal Tax ID {inv['tax_id']}")
    c.setFont("Helvetica-Bold", 13)
    c.drawString(50, 670, "INVOICE")
    c.setFont("Helvetica", 10)
    y = 650
    meta = [("Invoice Number", inv.get("inv_no")), ("Date Issued", fmt_date(inv["inv_date"], "gb") if inv.get("inv_date") else None),
            ("Your Reference", inv.get("po_ref"))]
    for lab, val in meta:
        if val:
            c.drawString(50, y, lab)
            c.drawString(170, y, val)
            y -= 15
    ty = _table(c, inv, 560, ["Description", "Qty", "Price", "Line Total"], [50, 350, 450, 562])
    labels = ["Net", "Tax ({pct})", "Total (incl. tax)" if inv.get("embed_tax") else "Grand Total"]
    ty = _totals(c, inv, ty, labels)
    c.setFont("Helvetica", 9)
    c.drawString(50, 110, f"Account ending {inv['bank_last4']}")


TEMPLATES = {"A": draw_A, "B": draw_B, "C": draw_C}


def scan_effect(pdf_path: Path, seed: int = 7):
    """Turn a digital PDF into an image-only PDF with skew, noise and blur (a phone scan of a printout)."""
    rng = np.random.default_rng(seed)
    page = pdfium.PdfDocument(str(pdf_path))[0]
    img = page.render(scale=200 / 72).to_pil().convert("L")
    img = img.rotate(0.6, resample=Image.BICUBIC, expand=False, fillcolor=255)
    arr = np.asarray(img, dtype=np.float32)
    arr = arr * 0.93 + 8
    arr += rng.normal(0, 7, arr.shape)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.7))
    img.save(pdf_path, "PDF", resolution=200)


def messy_effect(pdf_path: Path, kind: str):
    """Make a deliberately hard-to-read copy so OCR has real work to do.

    photo: phone photo of a printout, tilted, with a shadow across it and a coffee ring.
    fax:   black and white fax, low resolution, with dropped lines and streaks.
    """
    rng = np.random.default_rng(11 if kind == "photo" else 23)
    page = pdfium.PdfDocument(str(pdf_path))[0]
    img = page.render(scale=(170 if kind == "photo" else 150) / 72).to_pil().convert("L")
    w, h = img.size
    if kind == "photo":
        img = img.rotate(-2.4, resample=Image.BICUBIC, expand=True, fillcolor=205)
        arr = np.asarray(img, dtype=np.float32)
        H, W = arr.shape
        yy, xx = np.mgrid[0:H, 0:W]
        arr *= 0.80 + 0.20 * (xx / W)                      # shadow falling from the left
        arr *= 1 - 0.12 * np.exp(-((yy - H * 0.35) ** 2 + (xx - W * 0.78) ** 2) / (2 * (min(H, W) * 0.09) ** 2))  # coffee ring
        arr = arr * 0.9 + 14 + rng.normal(0, 9, arr.shape)
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.9))
        img.save(pdf_path, "PDF", resolution=170)
    else:
        img = img.rotate(0.9, resample=Image.BICUBIC, expand=False, fillcolor=255)
        arr = np.asarray(img, dtype=np.float32) + rng.normal(0, 12, (img.size[1], img.size[0]))
        for y in rng.choice(arr.shape[0], size=arr.shape[0] // 60, replace=False):   # dropped scan lines
            arr[y:y + 1, :] = 255
        for x in (int(arr.shape[1] * 0.56), int(arr.shape[1] * 0.97)):               # toner streaks
            arr[:, x:x + 1] -= 70
        bw = (arr > 150).astype(np.uint8) * 255
        Image.fromarray(bw).convert("1").save(pdf_path, "PDF", resolution=150)


def build_invoice(path: Path, *, vendor_id=None, vendor_override=None, template, inv_no, inv_date, po_ref, lines=None,
                  po_for_lines=None, tax_rate=None, total_override=None, embed_tax=False, bank=None, scanned=False,
                  omit_number=False, omit_date=False, messy=None):
    if vendor_id:
        v = V[vendor_id]
        vinfo = dict(vendor_name=v[1], tax_id=v[2], bank_last4=v[3], address=v[6])
    else:
        vinfo = dict(vendor_override)
    if vendor_override and vendor_id:
        vinfo.update(vendor_override)
    if bank:
        vinfo["bank_last4"] = bank
    if po_for_lines:
        lines, rate = lines_from_po(po_for_lines)
    else:
        rate = tax_rate or 0.0
    lines = [(dsc, q, u, round(q * u, 2) if amt is None else amt) for dsc, q, u, amt in lines]
    sub = round(sum(l[3] for l in lines), 2)
    tax = round(sub * rate, 2)
    total = round(sub + tax, 2) if total_override is None else total_override
    inv = dict(vinfo, template=template, inv_no=None if omit_number else inv_no,
               inv_date=None if omit_date else inv_date, due_date=inv_date + timedelta(days=30), po_ref=po_ref,
               lines=lines, subtotal=sub, tax_rate=rate, tax=tax, total=total, embed_tax=embed_tax)
    c = canvas.Canvas(str(path), pagesize=letter)
    TEMPLATES[template](c, inv)
    c.showPage()
    c.save()
    if scanned:
        scan_effect(path)
    if messy:
        messy_effect(path, messy)
    return dict(vendor_name=vinfo["vendor_name"], tax_id=vinfo["tax_id"], bank_last4=vinfo["bank_last4"],
                invoice_number=inv["inv_no"], invoice_date=inv["inv_date"].isoformat() if inv["inv_date"] else None,
                po_number=po_ref, total=total, subtotal=None if embed_tax else sub,
                tax=None if embed_tax else tax, line_count=len(lines))


RECEIPT_PHOTO = config.BASE_DIR / "assets" / "card_receipt.jpg"


def receipt_sample(out_dir: Path = config.SAMPLES_DIR) -> dict:
    """A phone photo of a restaurant card receipt, wrapped as a one page PDF. It must never be paid as an invoice."""
    from app import images
    f = out_dir / "card_receipt.pdf"
    f.write_bytes(images.to_pdf(RECEIPT_PHOTO.read_bytes()))
    return dict(id="card_receipt", title="Card receipt, not an invoice",
                description="A phone photo of a restaurant card receipt. It was already paid by card, so it must be refused, not paid again.",
                category="not_invoice", file=f.name, expected_decision="REJECTED", expected_codes=["RECEIPT_NOT_INVOICE"],
                truth=dict(vendor_name="Main Street Restaurant", total=29.01), scanned=True)


def messy_samples(out_dir: Path = config.SAMPLES_DIR) -> list[dict]:
    """Two hard-to-read copies of real invoices. OCR has to read them, and the numbers still have to add up."""
    out = []
    for sid, title, desc, kind, kw in [
        ("messy_photo", "Messy phone photo", "A tilted phone photo of a printout with a shadow and a coffee ring. OCR has to read it, then every figure is checked as usual.",
         "photo", dict(vendor_id="V005", template="A", inv_no="INV-MP-5560", inv_date=d(-3), po_ref="PO-4508", po_for_lines="PO-4508")),
        ("messy_fax", "Messy fax copy", "A low resolution black and white fax with streaks and dropped lines. OCR misreads one figure, the arithmetic check fails, and it goes to a person instead of being paid.",
         "fax", dict(vendor_id="V002", template="B", inv_no="INV-BL-2330", inv_date=d(-2), po_ref="PO-4502",
                     lines=[("Freight and logistics services Q4, December", 1, 2500.00, 2500.00)])),
    ]:
        f = out_dir / f"{sid}.pdf"
        truth = build_invoice(f, messy=kind, **kw)
        out.append(dict(id=sid, title=title, description=desc, category="messy", file=f.name,
                        expected_decision="APPROVED" if kind == "photo" else "HOLD",
                        expected_codes=[] if kind == "photo" else ["MATH_MISMATCH"], truth=truth, scanned=True))
    return out


def ensure_messy_samples(out_dir: Path = config.SAMPLES_DIR) -> None:
    """Older data folders were generated before these existed. Add them without touching anything else."""
    mf = out_dir / "manifest.json"
    if not mf.exists():
        return
    data = json.loads(mf.read_text())
    have = {s["id"] for s in data["samples"]}
    if {"messy_photo", "messy_fax"} <= have and all((out_dir / f"{i}.pdf").exists() for i in ("messy_photo", "messy_fax")):
        return
    data["samples"] = [s for s in data["samples"] if s["id"] not in ("messy_photo", "messy_fax")] + messy_samples(out_dir)
    mf.write_text(json.dumps(data, indent=2))


def ensure_receipt_sample(out_dir: Path = config.SAMPLES_DIR) -> None:
    """Older data folders were generated before this sample existed. Add it without regenerating anything else."""
    mf = out_dir / "manifest.json"
    if not mf.exists() or not RECEIPT_PHOTO.exists():
        return
    data = json.loads(mf.read_text())
    if any(s["id"] == "card_receipt" for s in data["samples"]) and (out_dir / "card_receipt.pdf").exists():
        return
    data["samples"] = [s for s in data["samples"] if s["id"] != "card_receipt"] + [receipt_sample(out_dir)]
    mf.write_text(json.dumps(data, indent=2))


def main(out_dir: Path = config.SAMPLES_DIR):
    out_dir.mkdir(parents=True, exist_ok=True)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_workbook(config.MASTER_XLSX)
    samples = []

    def add(sid, title, desc, category, expect, codes, **kw):
        f = out_dir / f"{sid}.pdf"
        truth = build_invoice(f, **kw)
        samples.append(dict(id=sid, title=title, description=desc, category=category, file=f.name,
                            expected_decision=expect, expected_codes=codes, truth=truth,
                            scanned=bool(kw.get("scanned"))))

    add("happy_clean", "Clean match", "Digital PDF, explicit PO, every line and tax figure agrees with PO-4501.",
        "happy", "APPROVED", [], vendor_id="V001", template="A", inv_no="INV-NW-20931", inv_date=d(-6),
        po_ref="PO-4501", po_for_lines="PO-4501")
    add("bundled_tax", "Bundled lines, tax embedded", "One bundled line and a single gross total. Nothing to itemise, so it compares gross to gross.",
        "happy", "APPROVED", ["BUNDLED_LINES", "TAX_EMBEDDED"], vendor_id="V003", template="C", inv_no="INV-HX-1102",
        inv_date=d(-4), po_ref="PO-4507", embed_tax=True, tax_rate=0.10,
        lines=[("Office supplies as per PO-4507", 1, 1600.00, 1600.00)])
    add("split_ok", "Split PO: final instalment", "Third invoice against PO-4503. Two earlier invoices are already on the ledger and this one lands exactly on the PO total.",
        "edge", "APPROVED", ["SPLIT_PO"], vendor_id="V003", template="C", inv_no="INV-HX-1003", inv_date=d(-3),
        po_ref="PO-4503", lines=[("Office furniture, phase 3 delivery", 1, 12000.00, 12000.00)])
    add("split_over", "Split PO: pushes cumulative billing over", "Each invoice looks reasonable alone. Together with the ledger it takes PO-4503 to 108% of its value.",
        "edge", "HOLD", ["OVER_PO_REVIEW", "SPLIT_PO"], vendor_id="V003", template="C", inv_no="INV-HX-1004", inv_date=d(-3),
        po_ref="PO-4503", lines=[("Office furniture, phase 3 delivery", 1, 14500.00, 14500.00)])
    add("tolerance", "Close but not exact", "Subscription plus an unplanned usage overage: 1.5% over PO-4504, inside the auto-approve band.",
        "edge", "APPROVED_WITH_NOTE", ["TOLERANCE_VARIANCE", "LINE_NOT_ON_PO"], vendor_id="V004", template="B",
        inv_no="INV-OC-88231", inv_date=d(-5), po_ref="PO-4504",
        lines=[("Annual platform subscription", 1, 24000.00, 24000.00), ("Usage overage (October)", 1, 360.00, 360.00)])
    add("scanned_implied_po", "Scanned image, no PO number", "Image-only PDF, skewed and noisy. No PO reference anywhere, so the process must infer PO-4505 from vendor, amounts and line items.",
        "edge", "APPROVED_WITH_NOTE", ["PO_INFERRED"], vendor_id="V005", template="B", inv_no="INV-MP-5530",
        inv_date=d(-8), po_ref=None, po_for_lines="PO-4505", scanned=True)
    add("missing_fields", "Missing invoice number and date", "A real invoice for PO-4502 with the invoice number and date blank. Cannot be paid, but the vendor can fix it in one email.",
        "edge", "HOLD", ["MISSING_INVOICE_NUMBER", "MISSING_INVOICE_DATE"], vendor_id="V002", template="B",
        inv_no="INV-BL-2301", inv_date=d(-2), po_ref="PO-4502", omit_number=True, omit_date=True,
        lines=[("Freight and logistics services, November", 1, 3500.00, 3500.00)])
    add("dup_exact", "Duplicate: same invoice sent twice", "INV-BL-2207 is already paid. The vendor emailed the identical PDF again.",
        "edge", "REJECTED", ["DUPLICATE_EXACT"], vendor_id="V002", template="B", inv_no="INV-BL-2207", inv_date=d(-25),
        po_ref="PO-4502", lines=[("Freight and logistics services, October", 1, 4000.00, 4000.00)])
    add("dup_near", "Duplicate: new number, same invoice", "Re-issued as INV-BL-2207A three days later. Same PO, amount and line items, so it is almost certainly the same bill.",
        "edge", "REJECTED", ["DUPLICATE_NEAR"], vendor_id="V002", template="B", inv_no="INV-BL-2207A", inv_date=d(-22),
        po_ref="PO-4502", lines=[("Freight and logistics services, October", 1, 4000.00, 4000.00)])
    add("lookalike_bank", "Lookalike vendor, changed bank account", "Everything matches PO-4506 to the cent. But the name is one letter off and the bank account is not the one on file.",
        "edge", "ESCALATE", ["BANK_MISMATCH", "VENDOR_NAME_VARIANT"], vendor_id="V001", template="A", inv_no="INV-NW-20944",
        inv_date=d(-2), po_ref="PO-4506", po_for_lines="PO-4506", bank="5512",
        vendor_override=dict(vendor_name="Northwind Industrial Supplies LLC"))
    add("unknown_vendor", "Vendor not on the approved list", "Invoice from a company that has never been onboarded, with no PO.",
        "edge", "REJECTED", ["VENDOR_NOT_APPROVED"], template="C", inv_no="AGP-7781", inv_date=d(-5), po_ref=None,
        vendor_override=dict(vendor_name="Apex Global Partners LLC", tax_id="83-9051127", bank_last4="2290",
                             address=["77 Commerce Drive", "Austin, TX 78701"]),
        lines=[("Consulting services", 1, 5200.00, 5200.00)])
    add("math_mismatch", "Arithmetic does not add up", "Lines and tax sum to 945.00 but the stated total is 995.00.",
        "edge", "HOLD", ["MATH_MISMATCH"], vendor_id="V005", template="A", inv_no="INV-MP-5541", inv_date=d(-3),
        po_ref="PO-4508", po_for_lines="PO-4508", total_override=995.00)

    if RECEIPT_PHOTO.exists():
        samples.append(receipt_sample(out_dir))
    samples.extend(messy_samples(out_dir))
    (out_dir / "manifest.json").write_text(json.dumps(
        dict(generated_for=TODAY.isoformat(), samples=samples), indent=2))
    # Ledger seeds: invoices that were already processed and paid before the demo starts.
    seed = [
        dict(po="PO-4502", vendor="V002", inv="INV-BL-2207", amount=4000.00, date=d(-25).isoformat(),
             lines=[("Freight and logistics services, October", 1)]),
        dict(po="PO-4503", vendor="V003", inv="INV-HX-1001", amount=9000.00, date=d(-38).isoformat(),
             lines=[("Office furniture, phase 1 delivery", 1)]),
        dict(po="PO-4503", vendor="V003", inv="INV-HX-1002", amount=9000.00, date=d(-18).isoformat(),
             lines=[("Office furniture, phase 2 delivery", 1)]),
    ]
    (config.DATA_DIR / "ledger_seed.json").write_text(json.dumps(seed, indent=2))
    print(f"Generated {len(samples)} sample invoices in {out_dir}")


if __name__ == "__main__":
    random.seed(1)
    main()
