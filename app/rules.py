"""Deterministic rules engine. The LLM may help read an invoice; it never makes the payment decision.

Every rule returns a Check carrying the evidence it used, so any decision can be explained line by line.
Outcome ladder (worst wins):  pass/info < warn < hold < reject < escalate
    -> APPROVED / APPROVED_WITH_NOTE / HOLD / REJECTED / ESCALATE
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

from rapidfuzz import fuzz

from . import config, db
from .extract import Extraction, norm_invoice_no
from .master import PO, Master, Vendor, norm_name

RANK = {"pass": 0, "info": 0, "warn": 1, "hold": 2, "reject": 3, "escalate": 4}
DECISION = {0: "APPROVED", 1: "APPROVED_WITH_NOTE", 2: "HOLD", 3: "REJECTED", 4: "ESCALATE"}


@dataclass
class Check:
    stage: str
    code: str
    title: str
    outcome: str
    message: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self):
        return dict(stage=self.stage, code=self.code, title=self.title, outcome=self.outcome,
                    message=self.message, evidence=self.evidence)


def chk(stage, code, title, outcome, message, **evidence) -> Check:
    return Check(stage, code, title, outcome, message, evidence)


@dataclass
class Ctx:
    ex: Extraction
    master: Master
    vendor: Optional[Vendor] = None
    vendor_how: str = ""
    po: Optional[PO] = None
    po_how: str = ""
    po_conf: Optional[float] = None
    cumulative: dict = field(default_factory=dict)
    candidates: list = field(default_factory=list)


def m2(x) -> str:
    return f"{x:,.2f}"


def _d(s) -> Optional[date]:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


# =========================================================================== VALIDATE
def validate(ctx: Ctx) -> list[Check]:
    ex, out = ctx.ex, []
    req = {"invoice_number": ("MISSING_INVOICE_NUMBER", "Invoice number", "It cannot be paid or checked for duplicates without one."),
           "invoice_date": ("MISSING_INVOICE_DATE", "Invoice date", "Without it the invoice cannot be checked against the PO dates."),
           "total": ("MISSING_TOTAL", "Total amount", "Without it there is nothing to match against the PO."),
           "vendor_name": ("MISSING_VENDOR", "Vendor name", "Without it the vendor cannot be verified.")}
    missing = []
    for name, (code, label, why) in req.items():
        if ex.get(name) is None:
            missing.append(label)
            out.append(chk("validate", code, f"{label} present", "hold",
                           f"{label} could not be found on the invoice. {why}", field=name))
    if not missing:
        out.append(chk("validate", "REQUIRED_FIELDS", "Required fields present", "pass",
                       "Invoice number, date, total and vendor were all found.", fields=list(req)))

    low = [(n, round(ex.conf(n), 2)) for n in config.CRITICAL_FIELDS if ex.get(n) is not None and ex.conf(n) < config.MIN_FIELD_CONFIDENCE]
    if low:
        out.append(chk("validate", "LOW_CONFIDENCE", "Field confidence", "hold",
                       "Too unsure about " + ", ".join(f"{n.replace('_', ' ')} ({c:.0%})" for n, c in low) + ". A person should confirm before any money moves.",
                       fields=dict(low), threshold=config.MIN_FIELD_CONFIDENCE))
    else:
        present = [ex.conf(n) for n in config.CRITICAL_FIELDS if ex.get(n) is not None]
        out.append(chk("validate", "FIELD_CONFIDENCE", "Field confidence", "pass",
                       f"Every critical field read with at least {min(present):.0%} confidence." if present else "No fields to score.",
                       lowest=round(min(present), 2) if present else None, threshold=config.MIN_FIELD_CONFIDENCE))

    sub, tax, tot = ex.get("subtotal"), ex.get("tax"), ex.get("total")
    if tot is not None:
        if sub is not None and tax is not None:
            diff = round(sub + tax - tot, 2)
            if abs(diff) > config.MATH_TOLERANCE_ABS:
                out.append(chk("validate", "MATH_MISMATCH", "Arithmetic", "hold",
                               f"Subtotal {m2(sub)} + tax {m2(tax)} = {m2(sub + tax)}, but the invoice states a total of {m2(tot)} (off by {m2(abs(diff))}).",
                               subtotal=sub, tax=tax, stated_total=tot, computed_total=round(sub + tax, 2)))
            else:
                out.append(chk("validate", "ARITHMETIC", "Arithmetic", "pass",
                               f"Subtotal {m2(sub)} + tax {m2(tax)} = {m2(tot)}, as stated.", subtotal=sub, tax=tax, total=tot))
        elif sub is None and tax is None:
            out.append(chk("validate", "TAX_EMBEDDED", "Tax treatment", "info",
                           "No separate subtotal or tax line, so tax is embedded in the total. It will be compared to the PO gross to gross.",
                           total=tot))
        if ex.lines and sub is not None:
            ls = round(sum(i["amount"] for i in ex.lines), 2)
            if abs(ls - sub) > config.MATH_TOLERANCE_ABS:
                out.append(chk("validate", "LINES_SUBTOTAL_MISMATCH", "Line items", "hold",
                               f"Line items add up to {m2(ls)} but the subtotal says {m2(sub)}.", lines_sum=ls, subtotal=sub))

    idate = _d(ex.get("invoice_date"))
    if idate:
        if idate > config.today() + timedelta(days=1):
            out.append(chk("validate", "FUTURE_DATED", "Invoice date", "hold", f"Invoice is dated {idate}, in the future.", date=str(idate)))
        elif idate < config.today() - timedelta(days=365):
            out.append(chk("validate", "STALE_INVOICE", "Invoice date", "warn", f"Invoice is more than a year old ({idate}).", date=str(idate)))
        else:
            out.append(chk("validate", "DATE_SANITY", "Invoice date", "pass", f"Dated {idate}, which is plausible.", date=str(idate)))
    return out


# =========================================================================== VENDOR
def check_vendor(ctx: Ctx) -> list[Check]:
    ex, m, out = ctx.ex, ctx.master, []
    name, tax, bank = ex.get("vendor_name"), ex.get("tax_id"), ex.get("bank_last4")
    vendor = m.vendor_by_tax(tax)
    how = "tax ID" if vendor else ""
    scored = sorted(((fuzz.ratio(norm_name(name), norm_name(v.name)), v) for v in m.vendors.values()), key=lambda t: -t[0]) if name else []
    best_score, best_v = scored[0] if scored else (0, None)

    if not vendor:
        if best_v and not tax and best_score >= config.NAME_RESOLVE_THRESHOLD:
            vendor, how = best_v, "name match"
            out.append(chk("vendor", "VENDOR_RESOLVED_BY_NAME", "Vendor identity", "warn",
                           f"No tax ID on the invoice. Matched '{name}' to '{best_v.name}' by name only ({best_score:.0f}% similar).",
                           score=round(best_score)))
        elif best_v and best_score >= config.NAME_VARIANT_THRESHOLD:
            out.append(chk("vendor", "VENDOR_IMPERSONATION_RISK", "Vendor identity", "escalate",
                           f"'{name}' closely resembles approved vendor '{best_v.name}' ({best_score:.0f}% similar) but tax ID {tax or '(missing)'} does not match the one on file.",
                           invoice_name=name, closest_vendor=best_v.name, score=round(best_score), tax_id=tax))
            return out
        else:
            out.append(chk("vendor", "VENDOR_NOT_APPROVED", "Approved vendor", "reject",
                           f"'{name or 'Unknown vendor'}' (tax ID {tax or 'not shown'}) is not on the approved vendor list. Closest match is only {best_score:.0f}% similar.",
                           invoice_name=name, tax_id=tax, closest=best_v.name if best_v else None, score=round(best_score)))
            return out
    ctx.vendor, ctx.vendor_how = vendor, how

    if vendor.status != "approved":
        out.append(chk("vendor", "VENDOR_BLOCKED", "Approved vendor", "reject",
                       f"{vendor.name} is on the vendor register with status '{vendor.status}'. No payments are allowed.", vendor_id=vendor.id, status=vendor.status))
        return out
    out.append(chk("vendor", "VENDOR_APPROVED", "Approved vendor", "pass",
                   f"{vendor.name} ({vendor.id}) is approved, identified by {how}.", vendor_id=vendor.id, resolved_by=how))

    if norm_name(name) == norm_name(vendor.name):
        out.append(chk("vendor", "VENDOR_NAME", "Vendor name", "pass", "Name on the invoice matches the vendor register.", invoice=name))
    else:
        out.append(chk("vendor", "VENDOR_NAME_VARIANT", "Vendor name", "warn",
                       f"Invoice says '{name}' but the register has '{vendor.name}' ({fuzz.ratio(norm_name(name), norm_name(vendor.name)):.0f}% similar).",
                       invoice=name, register=vendor.name))

    if bank and vendor.bank_last4 and bank != vendor.bank_last4:
        out.append(chk("vendor", "BANK_MISMATCH", "Bank account", "escalate",
                       f"The invoice asks for payment to an account ending {bank}, but the vendor register has an account ending {vendor.bank_last4}. This is the classic payment-redirection pattern. Do not pay until the change is verified by phone with a known contact.",
                       invoice_last4=bank, register_last4=vendor.bank_last4))
    elif bank and vendor.bank_last4:
        out.append(chk("vendor", "BANK_MATCH", "Bank account", "pass", f"Account ending {bank} matches the register.", last4=bank))
    else:
        out.append(chk("vendor", "BANK_NOT_ON_INVOICE", "Bank account", "info", "No bank details on the invoice, so nothing to verify.", ))
    return out


# =========================================================================== DUPLICATES
def check_duplicates(ctx: Ctx) -> list[Check]:
    ex = ctx.ex
    if not ctx.vendor:
        return [chk("duplicates", "DUP_SKIPPED", "Duplicate scan", "info", "Skipped: the vendor could not be resolved.")]
    prior = db.ledger_for_vendor(ctx.vendor.id)
    inv_no, total, idate = norm_invoice_no(ex.get("invoice_number")), ex.get("total"), _d(ex.get("invoice_date"))
    descr = " ".join(i["description"] for i in ex.lines)
    out, flagged = [], set()

    for r in prior:
        if inv_no and r["inv_norm"] == inv_no:
            flagged.add(r["id"])
            out.append(chk("duplicates", "DUPLICATE_EXACT", "Exact duplicate", "reject",
                           f"Invoice {r['invoice_number']} from this vendor was already processed ({m2(r['amount'])}, status {r['status']}). Paying it again would be a double payment.",
                           original=r["invoice_number"], amount=r["amount"], date=r["invoice_date"], status=r["status"]))
            break

    if total is not None:
        for r in prior:
            if r["id"] in flagged or abs(r["amount"] - total) > 0.01:
                continue
            rd = _d(r["invoice_date"])
            delta = abs((idate - rd).days) if idate and rd else None
            if delta is not None and delta > config.DUP_WINDOW_DAYS:
                continue
            sim = fuzz.token_set_ratio(descr, r["descr"] or "") if descr else 0
            ev = dict(original=r["invoice_number"], amount=r["amount"], days_apart=delta, description_similarity=round(sim))
            ref = r["invoice_number"] or "an earlier invoice that has no number"
            if sim >= 90 and delta is not None:
                out.append(chk("duplicates", "DUPLICATE_NEAR", "Near duplicate", "reject",
                               f"Different invoice number, but the same vendor, amount ({m2(total)}) and line items as {ref} from {delta} day(s) apart. Almost certainly the same bill re-issued.", **ev))
            else:
                out.append(chk("duplicates", "DUPLICATE_SUSPECT", "Possible duplicate", "hold",
                               f"Same vendor and amount ({m2(total)}) as {ref}, but the line items differ. A person should decide whether it is a new charge.", **ev))
            break

    if not out:
        out.append(chk("duplicates", "NO_DUPLICATE", "Duplicate scan", "pass",
                       f"Compared against {len(prior)} earlier invoice(s) from this vendor by number, amount, date and line items. No match.",
                       compared=len(prior)))
    return out


# =========================================================================== MATCH
def _best_line(desc: str, po: PO):
    return max(((fuzz.token_set_ratio(desc, l["description"]), l) for l in po.lines), key=lambda t: t[0])


def _infer_po(ctx: Ctx) -> list[Check]:
    ex, total = ctx.ex, ctx.ex.get("total")
    cands = []
    for po in ctx.master.open_pos_for(ctx.vendor.id):
        billed = sum(r["amount"] for r in db.ledger_for_po(po.number))
        remaining = po.total - billed
        if remaining <= 0.005:
            continue
        if total is None:
            amount_score = 0.0
        elif abs(total - remaining) / remaining <= 0.005 or abs(total - po.total) / po.total <= 0.005:
            amount_score = 1.0
        elif total <= remaining:
            amount_score = 0.4
        else:
            amount_score = 0.0
        line_score = 0.0
        if ex.lines and po.lines:
            hits = sum(1 for l in ex.lines if _best_line(l["description"], po)[0] >= 85)
            line_score = hits / len(ex.lines)
        conf = round(0.3 + 0.4 * amount_score + 0.3 * line_score, 3)
        cands.append(dict(po=po.number, description=po.description, confidence=conf, amount_score=amount_score,
                          line_score=round(line_score, 2), remaining=round(remaining, 2)))
    cands.sort(key=lambda c: -c["confidence"])
    ctx.candidates = cands
    if not cands:
        return [chk("match", "PO_NOT_FOUND", "PO match", "hold", "No PO is quoted on the invoice and this vendor has no open PO with a balance.")]
    best = cands[0]
    margin = best["confidence"] - (cands[1]["confidence"] if len(cands) > 1 else 0.0)
    ev = dict(candidates=cands[:3], margin=round(margin, 2))
    if best["confidence"] >= config.IMPLIED_PO_APPROVE_CONF and margin >= config.IMPLIED_PO_MIN_MARGIN:
        ctx.po, ctx.po_how, ctx.po_conf = ctx.master.pos[best["po"]], "inferred", best["confidence"]
        return [chk("match", "PO_INFERRED", "PO match", "warn",
                    f"No PO number on the invoice. Inferred {best['po']} ({best['description']}) with {best['confidence']:.0%} confidence: the vendor, total and {best['line_score']:.0%} of line items all agree, and it beats the next candidate by {margin:.0%}. Noted for the buyer.", **ev)]
    if best["confidence"] >= 0.6 and margin >= config.IMPLIED_PO_MIN_MARGIN:
        return [chk("match", "PO_INFERRED_LOW_CONFIDENCE", "PO match", "hold",
                    f"No PO number on the invoice. Best guess is {best['po']} at only {best['confidence']:.0%} confidence, which is below the {config.IMPLIED_PO_APPROVE_CONF:.0%} needed to infer it automatically.", **ev)]
    return [chk("match", "PO_NOT_FOUND", "PO match", "hold",
                "No PO is quoted and no open PO for this vendor is a convincing match." if cands else "No PO found.", **ev)]


def _line_checks(ctx: Ctx, po: PO) -> list[Check]:
    lines = ctx.ex.lines
    if not lines:
        return []
    if not po.lines:
        return [chk("match", "NO_PO_LINES", "Line items", "info",
                    f"{po.number} was entered with a total only, so line items are not compared. The amount is still checked against the PO.")]
    if len(lines) == 1 and len(po.lines) > 1 and _best_line(lines[0]["description"], po)[0] < 70:
        return [chk("match", "BUNDLED_LINES", "Line items", "info",
                    f"The invoice bundles everything into one line, so {len(po.lines)} PO lines cannot be checked individually. Matched on the total instead.",
                    invoice_lines=1, po_lines=len(po.lines))]
    if len(po.lines) == 1:
        extra = [l for l in lines if _best_line(l["description"], po)[0] < 70]
        if extra:
            txt = "; ".join(f"'{l['description']}' ({m2(l['amount'])})" for l in extra)
            return [chk("match", "LINE_NOT_ON_PO", "Line items", "warn",
                        f"Charge not covered by the PO line ('{po.lines[0]['description']}'): {txt}.", extra=extra)]
        return [chk("match", "LINES_MATCH_PO", "Line items", "pass", "Invoice lines are consistent with what the PO describes.")]
    unmatched, price, qty = [], [], []
    for l in lines:
        score, pl = _best_line(l["description"], po)
        if score < 80:
            unmatched.append(l)
            continue
        if pl["unit_price"] and abs(l["unit_price"] - pl["unit_price"]) / pl["unit_price"] > config.LINE_PRICE_TOLERANCE_PCT:
            price.append(dict(line=l["description"], invoice=l["unit_price"], po=pl["unit_price"]))
        if l["qty"] > pl["qty"] + 1e-9:
            qty.append(dict(line=l["description"], invoice=l["qty"], po=pl["qty"]))
    out = []
    if unmatched:
        out.append(chk("match", "LINE_NOT_ON_PO", "Line items", "warn",
                       "Not on the PO: " + "; ".join(f"'{l['description']}' ({m2(l['amount'])})" for l in unmatched) + ".", lines=unmatched))
    if price:
        out.append(chk("match", "LINE_PRICE_VARIANCE", "Line items", "warn",
                       "Unit price differs from the PO: " + "; ".join(f"{p['line']} {p['invoice']} vs {p['po']}" for p in price) + ".", lines=price))
    if qty:
        out.append(chk("match", "LINE_QTY_EXCEEDS", "Line items", "hold",
                       "Billed quantity is above the PO quantity: " + "; ".join(f"{q['line']} {q['invoice']:g} vs {q['po']:g}" for q in qty) + ".", lines=qty))
    if not out:
        out.append(chk("match", "LINES_MATCH_PO", "Line items", "pass",
                       f"All {len(lines)} invoice lines match PO lines on description, quantity and unit price."))
    return out


def check_match(ctx: Ctx) -> list[Check]:
    ex, m, out = ctx.ex, ctx.master, []
    if not ctx.vendor:
        return [chk("match", "MATCH_SKIPPED", "PO match", "info", "Skipped: there is no approved vendor to match against.")]
    ref, total = ex.get("po_number"), ex.get("total")
    if ref:
        po = m.pos.get(ref)
        if po is None:
            out.append(chk("match", "PO_NOT_FOUND", "PO match", "hold", f"{ref} is quoted on the invoice but is not in the PO register.", po_ref=ref))
        elif po.vendor_id != ctx.vendor.id:
            return [chk("match", "PO_VENDOR_MISMATCH", "PO match", "hold",
                        f"{ref} belongs to a different vendor ({m.vendors[po.vendor_id].name}).", po_ref=ref)]
        else:
            ctx.po, ctx.po_how = po, "explicit"
            out.append(chk("match", "PO_FOUND", "PO match", "pass", f"{ref} is on the invoice and belongs to {ctx.vendor.name}.", po=ref, how="explicit"))
    if ctx.po is None:
        out += _infer_po(ctx)
    po = ctx.po
    if po is None:
        return out

    problems = []
    if po.status != "open":
        problems.append(chk("match", "PO_CLOSED", "PO validity", "hold", f"{po.number} is {po.status}.", status=po.status))
    if po.expiry_date < config.today():
        problems.append(chk("match", "PO_EXPIRED", "PO validity", "hold", f"{po.number} expired on {po.expiry_date}.", expiry=str(po.expiry_date)))
    idate = _d(ex.get("invoice_date"))
    if idate and idate < po.issue_date:
        problems.append(chk("match", "INVOICE_BEFORE_PO", "PO validity", "warn",
                            f"Invoice is dated {idate}, before {po.number} was issued on {po.issue_date}.", invoice=str(idate), po_issued=str(po.issue_date)))
    out += problems or [chk("match", "PO_VALID", "PO validity", "pass", f"{po.number} is open and valid until {po.expiry_date}.")]

    if total is not None:
        prior = db.ledger_for_po(po.number)
        billed = round(sum(r["amount"] for r in prior), 2)
        cum = round(billed + total, 2)
        over = round(cum - po.total, 2)
        pct = over / po.total if po.total else 0
        ctx.cumulative = dict(po_total=po.total, billed_before=billed, this_invoice=total, cumulative_after=cum,
                              remaining_after=round(po.total - cum, 2), prior_invoices=len(prior), over_by=max(over, 0), over_pct=round(max(pct, 0), 4))
        ev = dict(ctx.cumulative)
        if prior:
            tail = "which exactly uses up the PO." if abs(over) <= 0.005 else f"leaving {m2(po.total - cum)} on the PO." if over < 0 else f"which is {m2(over)} more than the PO."
            out.append(chk("match", "SPLIT_PO", "Split billing", "info",
                           f"This is invoice {len(prior) + 1} against {po.number}. {m2(billed)} is already billed across {len(prior)} invoice(s); this one brings the cumulative total to {m2(cum)} of {m2(po.total)}, {tail}", **ev))
        if abs(over) <= 0.005:
            out.append(chk("match", "AMOUNT_MATCH", "Amount vs PO", "pass", f"Cumulative billing {m2(cum)} equals the PO total {m2(po.total)}.", **ev))
        elif over < 0:
            out.append(chk("match", "PARTIAL_BILLING", "Amount vs PO", "pass",
                           f"Cumulative billing {m2(cum)} is within the PO total {m2(po.total)}; {m2(-over)} remains.", **ev))
        elif pct <= config.AUTO_TOLERANCE_PCT and over <= config.AUTO_TOLERANCE_ABS:
            out.append(chk("match", "TOLERANCE_VARIANCE", "Amount vs PO", "warn",
                           f"{m2(cum)} is {m2(over)} ({pct:.1%}) over the PO total {m2(po.total)}. That is inside the auto-approve tolerance of {config.AUTO_TOLERANCE_PCT:.0%} / {m2(config.AUTO_TOLERANCE_ABS)}, so it is approved with a note.", **ev))
        elif pct <= config.REVIEW_TOLERANCE_PCT:
            out.append(chk("match", "OVER_PO_REVIEW", "Amount vs PO", "hold",
                           f"Cumulative billing would reach {m2(cum)} against a PO of {m2(po.total)}: {m2(over)} ({pct:.1%}) over. Above the auto-approve tolerance, so a buyer must confirm or amend the PO.", **ev))
        else:
            out.append(chk("match", "OVER_PO_LIMIT", "Amount vs PO", "reject",
                           f"Cumulative billing would reach {m2(cum)} against a PO of {m2(po.total)}: {m2(over)} ({pct:.1%}) over, beyond the {config.REVIEW_TOLERANCE_PCT:.0%} review limit. Dispute with the vendor.", **ev))
    out += _line_checks(ctx, po)
    return out


# =========================================================================== DECIDE
VENDOR_FACING = {
    "MISSING_INVOICE_NUMBER": "the invoice number is missing",
    "MISSING_INVOICE_DATE": "the invoice date is missing",
    "MISSING_TOTAL": "the total amount could not be read",
    "MATH_MISMATCH": "the lines and tax do not add up to the stated total",
    "LINES_SUBTOTAL_MISMATCH": "the line items do not add up to the subtotal",
    "OVER_PO_LIMIT": "billing would exceed the purchase order value",
    "DUPLICATE_EXACT": "we have already processed this invoice",
    "DUPLICATE_NEAR": "it appears to duplicate an invoice we have already processed",
    "VENDOR_NOT_APPROVED": "your company is not yet set up as an approved vendor",
    "PO_NOT_FOUND": "we could not match it to a purchase order; please quote the PO number",
}
ACTIONS = {
    "MISSING_INVOICE_NUMBER": ("AP", "Ask the vendor for a corrected invoice, or type the missing fields in and re-run."),
    "MISSING_INVOICE_DATE": ("AP", "Ask the vendor for a corrected invoice, or type the missing fields in and re-run."),
    "MISSING_TOTAL": ("AP", "Ask the vendor for a legible copy."),
    "MATH_MISMATCH": ("AP", "Request a corrected invoice. Do not pay the stated total."),
    "LINES_SUBTOTAL_MISMATCH": ("AP", "Request a corrected invoice."),
    "OVER_PO_REVIEW": ("Buyer", "Confirm the overage is legitimate and amend the PO, or approve only the remaining PO balance."),
    "OVER_PO_LIMIT": ("AP", "Dispute with the vendor and request a credit note."),
    "DUPLICATE_EXACT": ("AP", "Do not pay. Tell the vendor it was already processed."),
    "DUPLICATE_NEAR": ("AP", "Do not pay. Confirm with the vendor that this is the same bill."),
    "DUPLICATE_SUSPECT": ("AP", "Check whether this is a genuinely separate charge before paying."),
    "BANK_MISMATCH": ("Treasury", "Block payment. Call the vendor on the number in the vendor register (never one printed on the invoice) and confirm any bank change."),
    "VENDOR_IMPERSONATION_RISK": ("Security", "Block payment and report to the fraud team. Do not reply to the sender."),
    "VENDOR_NOT_APPROVED": ("Procurement", "Send to vendor onboarding. No payment until approved."),
    "VENDOR_BLOCKED": ("Procurement", "Do not pay. Contact procurement about why this vendor is blocked."),
    "PO_NOT_FOUND": ("Buyer", "Identify the right PO, or confirm there is none."),
    "PO_INFERRED_LOW_CONFIDENCE": ("Buyer", "Confirm which PO this belongs to."),
    "PO_INFERRED": ("Buyer", "FYI: PO was inferred, not stated. Ask the vendor to quote the PO number on future invoices."),
    "TOLERANCE_VARIANCE": ("Buyer", "FYI: approved within tolerance. Check the extra charge is expected."),
    "LINE_NOT_ON_PO": ("Buyer", "Confirm the extra charge is expected."),
    "LOW_CONFIDENCE": ("AP", "Check the highlighted fields against the PDF, correct them and re-run."),
    "LINE_QTY_EXCEEDS": ("Buyer", "Confirm the extra quantity was ordered and received."),
    "UNREADABLE_DOCUMENT": ("AP", "Ask the vendor to resend a readable PDF."),
    "RECEIPT_NOT_INVOICE": ("AP", "Do not pay: it was already paid by card. If it is a business expense, send it to expenses with the cardholder."),
    "FUTURE_DATED": ("AP", "Confirm the invoice date with the vendor."),
}


def decide(ctx: Ctx, checks: list[Check]) -> dict:
    ex = ctx.ex
    worst = max((RANK[c.outcome] for c in checks), default=0)
    decision = DECISION[worst]
    drivers = [c for c in checks if RANK[c.outcome] == worst and worst > 0]
    flagged = [c for c in checks if RANK[c.outcome] >= 1]
    notes = [c for c in checks if c.outcome == "info"]
    passed = sum(1 for c in checks if c.outcome == "pass")
    inv_no = ex.get("invoice_number") or "(no number)"
    claimed = ex.get("vendor_name") or "unknown vendor"
    # For a suspected impersonation, name the sender as the invoice claims, not as the register spells it.
    vname = claimed if (worst == RANK["escalate"] or not ctx.vendor) else ctx.vendor.name
    msgs = [c.message for c in drivers]
    driver_text = " ".join(msgs[:2]) + (f" {len(msgs) - 2} more finding(s) are in the trace." if len(msgs) > 2 else "")
    tot = ex.get("total")
    amount = f"${m2(tot)}" if tot is not None else "an unreadable amount"
    po_txt = ctx.po.number if ctx.po else "no PO"

    if decision == "APPROVED":
        headline = "Approved for payment"
        explanation = f"Invoice {inv_no} from {vname} for {amount} matches {po_txt} on vendor, amounts and line items. All {passed} checks passed."
    elif decision == "APPROVED_WITH_NOTE":
        headline = "Approved with a note"
        explanation = f"Invoice {inv_no} from {vname} for {amount} is approved against {po_txt}, with {len(flagged)} point(s) noted for the buyer. " + driver_text
    elif decision == "HOLD":
        headline = "Held for human review"
        explanation = f"Invoice {inv_no} from {vname} for {amount} cannot be approved automatically. " + driver_text
    elif decision == "REJECTED":
        headline = "Rejected"
        explanation = f"Invoice {inv_no} from {vname} for {amount} will not be paid. " + driver_text
        if any(c.code == "RECEIPT_NOT_INVOICE" for c in drivers):
            headline = "Not an invoice"
            explanation = f"This is a card payment receipt from {claimed} for {amount}. It was already paid by card, so nothing will be paid."
    else:
        headline = "Escalated: payment blocked"
        explanation = f"Invoice {inv_no} from {vname} for {amount} shows signs of fraud and payment is blocked. " + driver_text

    seen, actions = set(), []
    for c in flagged:
        if c.code in ACTIONS and ACTIONS[c.code] not in seen:
            seen.add(ACTIONS[c.code])
            owner, act = ACTIONS[c.code]
            actions.append(dict(code=c.code, owner=owner, action=act))

    email = None
    if decision in ("HOLD", "REJECTED"):
        asks = [(c, VENDOR_FACING[c.code]) for c in flagged if c.code in VENDOR_FACING]
        if asks:
            vendor_email = ctx.vendor.email if ctx.vendor else "vendor contact on the invoice"
            lines = []
            for c, txt in asks:
                extra = ""
                if c.code in ("MATH_MISMATCH",):
                    extra = f" (lines and tax come to {m2(c.evidence.get('computed_total', 0))}, the invoice says {m2(c.evidence.get('stated_total', 0))})"
                lines.append(f"  - {txt}{extra}")
            email = dict(to=vendor_email, subject=f"Invoice {inv_no}: we need one thing before we can pay it",
                         body=f"Hello {vname} team,\n\nThanks for your invoice. We have not been able to process it yet because:\n" + "\n".join(lines)
                         + "\n\nPlease reply to this email with a corrected invoice, or let us know if you think we have misread it.\n\nKind regards,\nAccounts Payable, Larkspur Industries")
    elif decision == "ESCALATE":
        email = dict(to="treasury-security@larkspur.example", subject=f"URGENT: suspected payment redirection, invoice {inv_no}",
                     body=f"Invoice {inv_no} ({amount}) claiming to be from {claimed} has been blocked.\n\n" + "\n".join(f"  - {c.message}" for c in drivers)
                     + "\n\nDo NOT reply to the sender and do NOT use contact details printed on the invoice. Verify with the vendor using the phone number held in the vendor register.", internal=True)

    confs = [ex.conf(n) for n in config.CRITICAL_FIELDS if ex.get(n) is not None]
    if ctx.po_conf is not None:
        confs.append(ctx.po_conf)
    release = {"APPROVED": "Release on due date", "APPROVED_WITH_NOTE": "Release on due date", "HOLD": "Hold", "REJECTED": "Do not pay", "ESCALATE": "Blocked"}[decision]
    return dict(decision=decision, headline=headline, explanation=explanation, payment=release,
                reason_codes=[c.code for c in flagged], notes=[dict(code=c.code, message=c.message) for c in notes],
                reasons=[dict(code=c.code, outcome=c.outcome, message=c.message) for c in flagged],
                actions=actions, email=email, data_confidence=round(min(confs), 2) if confs else None,
                checks_passed=passed, checks_total=len(checks),
                po=dict(number=ctx.po.number, how=ctx.po_how, confidence=ctx.po_conf) if ctx.po else None,
                cumulative=ctx.cumulative or None,
                vendor=dict(id=ctx.vendor.id, name=ctx.vendor.name, resolved_by=ctx.vendor_how) if ctx.vendor else None)
