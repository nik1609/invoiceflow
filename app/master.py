"""Procurement master data, read from the spreadsheet an AP team would actually keep (vendors, POs, PO lines)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from openpyxl import load_workbook

from . import config, db


@dataclass
class Vendor:
    id: str
    name: str
    tax_id: str
    bank_last4: str
    status: str
    email: str


@dataclass
class PO:
    number: str
    vendor_id: str
    description: str
    currency: str
    subtotal: float
    tax_rate: float
    tax: float
    total: float
    issue_date: date
    expiry_date: date
    status: str
    lines: list[dict] = field(default_factory=list)


class Master:
    def __init__(self, vendors: dict[str, Vendor], pos: dict[str, PO]):
        self.vendors, self.pos = vendors, pos

    def vendor_by_tax(self, tax_id: Optional[str]) -> Optional[Vendor]:
        if not tax_id:
            return None
        return next((v for v in self.vendors.values() if v.tax_id == tax_id), None)

    def open_pos_for(self, vendor_id: str) -> list[PO]:
        return [p for p in self.pos.values() if p.vendor_id == vendor_id and p.status == "open"]


def _table(ws):
    it = ws.iter_rows(values_only=True)
    head = [str(h) for h in next(it)]
    return [dict(zip(head, r)) for r in it if any(c is not None for c in r)]


def load() -> Master:
    wb = load_workbook(config.MASTER_XLSX, data_only=True)
    vendors = {r["vendor_id"]: Vendor(r["vendor_id"], r["name"], str(r["tax_id"]), str(r["bank_last4"]), r["status"], r["email"])
               for r in _table(wb["Vendors"])}
    pos = {}
    for r in _table(wb["PurchaseOrders"]):
        pos[r["po_number"]] = PO(r["po_number"], r["vendor_id"], r["description"], r["currency"], float(r["subtotal"]),
                                 float(r["tax_rate"]), float(r["tax"]), float(r["total"]),
                                 date.fromisoformat(str(r["issue_date"])[:10]), date.fromisoformat(str(r["expiry_date"])[:10]),
                                 r["status"])
    for r in _table(wb["POLines"]):
        pos[r["po_number"]].lines.append(dict(line_no=r["line_no"], description=r["description"], qty=float(r["qty"]),
                                              unit_price=float(r["unit_price"]), amount=float(r["amount"])))
    # Vendors and POs added through the app live in the database and sit alongside the spreadsheet's.
    try:
        for r in db.custom_vendors():
            vendors[r["id"]] = Vendor(r["id"], r["name"], r["tax_id"], r["bank_last4"], r["status"], r["email"] or "")
        for r in db.custom_pos():
            pos[r["number"]] = PO(r["number"], r["vendor_id"], r["description"], r["currency"], r["total"], 0.0, 0.0, r["total"],
                                  date.fromisoformat(r["issue_date"]), date.fromisoformat(r["expiry_date"]), r["status"])
    except Exception:  # noqa: BLE001 - tables are created on startup; never let master data stop a run
        pass
    return Master(vendors, pos)


LEGAL_SUFFIX = re.compile(r"\b(llc|inc|ltd|co|corp|corporation|company|pvt|limited|plc)\b\.?", re.I)


def norm_name(s: Optional[str]) -> str:
    s = LEGAL_SUFFIX.sub("", (s or "").lower())
    return re.sub(r"[^a-z0-9 ]", "", s).strip()
