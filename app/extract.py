"""Invoice extraction.

Pipeline:  PDF -> text (digital layer, or OCR if the page is an image) -> field parser -> per-field confidence.
An optional LLM pass (set GROQ_API_KEY, or ANTHROPIC_API_KEY) is a second reader. It never decides anything. It may fill in a
field the parser could not read, but only with text that is really on the page, and disagreement lowers confidence.
"""
from __future__ import annotations

import io
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

import pdfplumber
import pypdfium2 as pdfium
import pytesseract
from PIL import Image

from . import config, settings

MONEY = r"\$?\s*([\d]{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})"
MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
    (re.compile(rf"\b(\d{{1,2}})\s+({MONTHS})[a-z]*\.?,?\s+(\d{{4}})\b", re.I), "dmy"),
    (re.compile(rf"\b({MONTHS})[a-z]*\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.I), "mdy"),
]


@dataclass
class FieldValue:
    value: Any = None
    confidence: float = 0.0
    source: str = ""          # which label / rule produced it
    note: str = ""
    box: Optional[dict] = None  # where it was found: {page, x, y, w, h} as fractions of the page

    def to_dict(self):
        return dict(value=self.value, confidence=round(self.confidence, 2), source=self.source, note=self.note, box=self.box)


@dataclass
class Extraction:
    method: str                                  # "digital" | "ocr"
    ocr_confidence: Optional[float]
    page_count: int
    text: str
    fields: dict[str, FieldValue] = field(default_factory=dict)
    lines: list[dict] = field(default_factory=list)
    llm: dict = field(default_factory=dict)      # status + agreement info when the LLM pass ran
    vision: dict = field(default_factory=dict)   # what the vision reader did for a weak scan, if it ran

    def get(self, name, default=None):
        fv = self.fields.get(name)
        return fv.value if fv and fv.value is not None else default

    def conf(self, name) -> float:
        fv = self.fields.get(name)
        return fv.confidence if fv else 0.0

    def to_dict(self):
        return dict(method=self.method, ocr_confidence=self.ocr_confidence, page_count=self.page_count,
                    fields={k: v.to_dict() for k, v in self.fields.items()}, lines=self.lines, llm=self.llm, vision=self.vision)


# --------------------------------------------------------------------------- text acquisition
def read_pdf(pdf_bytes: bytes):
    """Return (text, method, ocr_confidence, pages, words). Each word is {t, page, x, y, w, h} in page fractions."""
    words: list[dict] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = len(pdf.pages)
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        if len(text.strip()) >= 40:
            for n, pg in enumerate(pdf.pages):
                for w in pg.extract_words():
                    words.append(dict(t=w["text"], page=n, x=w["x0"] / pg.width, y=w["top"] / pg.height,
                                      w=(w["x1"] - w["x0"]) / pg.width, h=(w["bottom"] - w["top"]) / pg.height))
            return text, "digital", None, pages, words
    doc = pdfium.PdfDocument(pdf_bytes)
    parts, confs = [], []
    for n, page in enumerate(doc):
        scale = 300 / 72
        w0, h0 = page.get_size()
        scale = min(scale, 3300 / max(w0, h0))   # never OCR more than about 3300 px on the long side: slower for no gain
        img: Image.Image = page.render(scale=scale).to_pil().convert("L")
        W, H = img.size
        # One OCR pass gives both the words (with boxes) and the text lines, so the page is not read twice.
        data = pytesseract.image_to_data(img, config="--psm 6", output_type=pytesseract.Output.DICT)
        rows: dict[tuple, list[str]] = {}
        for i, (w, c) in enumerate(zip(data["text"], data["conf"])):
            if w.strip() and float(c) >= 0:
                confs.append(float(c))
                words.append(dict(t=w.strip(), page=n, x=data["left"][i] / W, y=data["top"][i] / H,
                                  w=data["width"][i] / W, h=data["height"][i] / H))
            if w.strip():
                rows.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(w.strip())
        parts.append("\n".join(" ".join(v) for v in rows.values()))
    mean = sum(confs) / len(confs) / 100 if confs else 0.0
    return "\n".join(parts), "ocr", round(mean, 3), len(doc), words


def pdf_to_text(pdf_bytes: bytes) -> tuple[str, str, Optional[float], int]:
    """Return (text, method, ocr_confidence, pages)."""
    return read_pdf(pdf_bytes)[:4]


# --------------------------------------------------------------------------- evidence: where on the page each value was read
def _lines_of(words: list[dict]) -> list[list[dict]]:
    """Group words into visual lines (same page, similar vertical centre), left to right."""
    out: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["page"], w["y"] + w["h"] / 2, w["x"])):
        cy = w["y"] + w["h"] / 2
        if out and out[-1][0]["page"] == w["page"] and abs((out[-1][0]["y"] + out[-1][0]["h"] / 2) - cy) < max(w["h"], 0.008) * 0.6:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(l, key=lambda w: w["x"]) for l in out]


def _union(ws: list[dict]) -> dict:
    x0, y0 = min(w["x"] for w in ws), min(w["y"] for w in ws)
    x1, y1 = max(w["x"] + w["w"] for w in ws), max(w["y"] + w["h"] for w in ws)
    return dict(page=ws[0]["page"], x=round(x0, 4), y=round(y0, 4), w=round(x1 - x0, 4), h=round(y1 - y0, 4))


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def locate_fields(fields: dict[str, FieldValue], words: list[dict]) -> None:
    """Attach a page box to every field whose value can be found among the words. Best effort, never raises."""
    if not words:
        return
    try:
        lines = _lines_of(words)

        def line_text(l):
            return " ".join(w["t"] for w in l).lower()

        def find_run(target: str, line):  # consecutive words on a line that spell the target
            tgt = _alnum(target)
            for i in range(len(line)):
                acc = ""
                for j in range(i, min(i + 6, len(line))):
                    acc += _alnum(line[j]["t"])
                    if acc == tgt:
                        return line[i:j + 1]
                    if not tgt.startswith(acc):
                        break
            return None

        def money_hits(amount: float):
            hits = []
            for l in lines:
                for w in l:
                    try:
                        if re.fullmatch(r"\$?[\d,]+\.\d{2}", w["t"]) and abs(to_float(w["t"]) - amount) < 0.005:
                            hits.append((l, w))
                    except ValueError:
                        pass
            return hits

        labels = {"total": ("total", "due"), "subtotal": ("subtotal", "sub-total", "sub total", "net"), "tax": ("tax", "vat", "gst")}
        for name, fv in fields.items():
            if fv.value is None or fv.source in ("human correction", "assumed") or fv.box:
                continue
            box = None
            if name in ("invoice_number", "po_number", "tax_id"):
                for l in lines:
                    run = find_run(str(fv.value), l)
                    if run:
                        box = _union(run)
                        break
            elif name == "bank_last4":
                for l in lines:
                    for w in l:
                        if str(fv.value) in w["t"] and re.search(r"\d{4}", w["t"]):
                            box = _union([w])
            elif name in labels:
                hits = money_hits(float(fv.value))
                scored = [h for h in hits if any(lb in line_text(h[0]) for lb in labels[name])]
                pick = (scored or hits or [None])[-1] if name == "total" else (scored or hits or [None])[0]
                if pick:
                    box = _union([pick[1]])
            elif name in ("invoice_date", "due_date"):
                want = fv.value if isinstance(fv.value, date) else parse_date(str(fv.value))
                for l in lines:
                    txt = line_text(l)
                    if name == "due_date" and "due" not in txt:
                        continue
                    if name == "invoice_date" and "due" in txt:
                        continue
                    for i in range(len(l)):
                        for j in range(i + 1, min(i + 4, len(l)) + 1):
                            if parse_date(" ".join(w["t"] for w in l[i:j])) == want:
                                box = _union(l[i:j])
                    if box:
                        break
            elif name == "vendor_name":
                toks = re.findall(r"[A-Za-z0-9&.'-]+", str(fv.value))
                for l in lines[:8]:
                    seq = [w for w in l if _alnum(w["t"]) and _alnum(w["t"]) in _alnum(str(fv.value))]
                    if len(seq) >= max(1, len(toks) - 1) and seq and seq[0]["y"] < 0.4:
                        box = _union(seq)
                        break
            if box:
                fv.box = box
    except Exception:  # noqa: BLE001 - evidence is a nicety; extraction must not fail because of it
        pass


# --------------------------------------------------------------------------- parsing helpers
def to_float(s: str) -> float:
    return float(s.replace(",", "").replace("$", "").strip())


def parse_date(s: str) -> Optional[date]:
    for rx, kind in DATE_PATTERNS:
        m = rx.search(s)
        if not m:
            continue
        try:
            if kind == "iso":
                return date(int(m[1]), int(m[2]), int(m[3]))
            if kind == "dmy":
                return datetime.strptime(f"{m[1]} {m[2][:3].title()} {m[3]}", "%d %b %Y").date()
            return datetime.strptime(f"{m[1][:3].title()} {m[2]} {m[3]}", "%b %d %Y").date()
        except ValueError:
            return None
    return None


def norm_invoice_no(s: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


TITLE_RX = re.compile(r"^\s*(tax\s+)?invoice\s*$", re.I)
INV_NO_RX = re.compile(
    r"(?:invoice\s*(?:no\.?|number|num|#)|inv\.?\s*(?:no\.?|number)|inv\s*#)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{2,})", re.I)
PO_RX = re.compile(
    r"(?:p\.?o\.?\s*(?:number|no\.?|#)?|purchase\s*order|customer\s*po|your\s*ref(?:erence)?)\s*[:#]?\s*(PO[-\s]?\d{3,})", re.I)
PO_ANY_RX = re.compile(r"\bPO[-\s]?(\d{3,})\b", re.I)
TAXID_RX = re.compile(r"\b(\d{2}-\d{7})\b")
BANK_RX = re.compile(r"(?:acct|account|a/c)\b[^0-9\n]{0,30}(\d{4})\b", re.I)

TOTAL_LABELS = [  # highest priority first
    (re.compile(r"(?:grand\s*total|total\s*due|amount\s*due|balance\s*due|total\s*\(incl\.?\s*tax\))\D{0,12}" + MONEY, re.I), 0.97),
    (re.compile(r"^\s*total\b(?!\s*\()\D{0,12}" + MONEY, re.I | re.M), 0.9),
]
SUBTOTAL_RX = re.compile(r"(?:sub-?\s*total|net)\b\s*" + MONEY, re.I)
TAX_RX = re.compile(r"(?:sales\s*tax|vat|gst|tax)\s*(?:\(\s*([\d.]+)\s*%\s*\))?\s*" + MONEY, re.I)
LINE_RX = re.compile(r"^(?P<desc>.+?)\s+(?P<qty>\d+(?:\.\d+)?)\s+\$?(?P<unit>[\d,]+\.\d{2})\s+\$?(?P<amt>[\d,]+\.\d{2})\s*$")


def _ocr_factor(method: str, ocr_conf: Optional[float]) -> float:
    return 1.0 if method == "digital" else max(0.0, min(1.0, (ocr_conf or 0.0) + 0.04))


def parse_fields(text: str, method: str, ocr_conf: Optional[float]) -> tuple[dict[str, FieldValue], list[dict]]:
    f: dict[str, FieldValue] = {}
    k = _ocr_factor(method, ocr_conf)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # vendor: first non-title line of the page
    vendor = None
    for ln in lines[:6]:
        clean = re.sub(r"\b(tax\s+)?invoice\b", "", ln, flags=re.I).strip(" -|")
        # A scan can fuse the vendor name with the metadata column on the same text row; cut at the first label.
        clean = re.split(r"\s+(?:inv\b|invoice\b|date\b|customer\b|terms\b|po\b|due\b)", clean, maxsplit=1, flags=re.I)[0].strip(" -|")
        if clean and not TITLE_RX.match(ln):
            vendor = clean
            break
    f["vendor_name"] = FieldValue(vendor, 0.9 * k if vendor else 0.0, "header")

    m = TAXID_RX.search(text)
    f["tax_id"] = FieldValue(m[1] if m else None, 0.97 * k if m else 0.0, "tax id pattern")
    bank = None
    for m in BANK_RX.finditer(text):
        bank = m[1]
    f["bank_last4"] = FieldValue(bank, 0.9 * k if bank else 0.0, "bank line")

    m = INV_NO_RX.search(text)
    f["invoice_number"] = FieldValue(m[1].strip("-/") if m else None, 0.96 * k if m else 0.0,
                                     "invoice number label" if m else "", "" if m else "no invoice number label found")

    # dates: skip any line whose label says "due"
    inv_date, label = None, ""
    for ln in lines:
        if re.search(r"\bdue\b", ln, re.I) or re.search(r"\bterms\b", ln, re.I):
            continue
        if re.search(r"\b(invoice\s*date|date\s*issued|issued|date)\b", ln, re.I):
            dt = parse_date(ln)
            if dt:
                inv_date, label = dt, ln.split()[0]
                break
    f["invoice_date"] = FieldValue(inv_date.isoformat() if inv_date else None, 0.95 * k if inv_date else 0.0,
                                   "date label" if inv_date else "", "" if inv_date else "no invoice date found")

    due = None
    for ln in lines:
        if re.search(r"\bdue\s*date\b", ln, re.I):
            due = parse_date(ln)
    f["due_date"] = FieldValue(due.isoformat() if due else None, 0.9 * k if due else 0.0, "due date label")

    m = PO_RX.search(text)
    po = re.sub(r"\s", "-", m[1].upper()).replace("PO", "PO-", 1).replace("PO--", "PO-") if m else None
    if not po:
        a = PO_ANY_RX.search(text)
        if a:
            po = f"PO-{a[1]}"
            f["po_number"] = FieldValue(po, 0.75 * k, "free-text mention")
    if "po_number" not in f:
        f["po_number"] = FieldValue(po, 0.96 * k if po else 0.0, "PO label" if po else "",
                                    "" if po else "no PO reference on invoice")

    # money
    total = None
    for rx, base in TOTAL_LABELS:
        mm = list(rx.finditer(text))
        if mm:
            total = (to_float(mm[-1].group(1)), base)
            break
    f["total"] = FieldValue(total[0] if total else None, total[1] * k if total else 0.0, "total label",
                            "" if total else "no total found")
    sm = SUBTOTAL_RX.search(text)
    f["subtotal"] = FieldValue(to_float(sm[1]) if sm else None, 0.95 * k if sm else 0.0, "subtotal label")
    tm = None
    for cand in TAX_RX.finditer(text):
        if "total" in text[max(0, cand.start() - 10):cand.start()].lower() and cand.group(1) is None:
            continue
        tm = cand
    f["tax"] = FieldValue(to_float(tm[2]) if tm else None, 0.95 * k if tm else 0.0, "tax label")
    f["tax_rate"] = FieldValue(float(tm[1]) / 100 if tm and tm[1] else None, 0.9 * k if tm and tm[1] else 0.0, "tax label")
    if re.search(r"\bUSD\b", text):
        f["currency"] = FieldValue("USD", 0.97 * k, "currency code")
    elif "$" in text:
        f["currency"] = FieldValue("USD", 0.9 * k, "dollar sign")
    else:  # these invoices print no symbol; the PO currency is the sensible default and is flagged as an assumption
        f["currency"] = FieldValue("USD", 0.6, "assumed", "no currency printed; assumed PO currency")

    items = []
    for ln in lines:
        mm = LINE_RX.match(ln)
        if mm and not re.search(r"\b(total|tax|subtotal|due)\b", mm["desc"], re.I) and not re.match(r"^\d{4}-", mm["desc"]):
            items.append(dict(description=mm["desc"].strip(), qty=float(mm["qty"]),
                              unit_price=to_float(mm["unit"]), amount=to_float(mm["amt"])))
    return f, items


# --------------------------------------------------------------------------- optional LLM cross-check
LLM_PROMPT = """Extract fields from this invoice text. Reply with JSON only, keys:
vendor_name, invoice_number, invoice_date (YYYY-MM-DD), po_number, total (number), subtotal (number), tax (number).
Use null when a field is not present. Do not guess.

INVOICE TEXT:
{text}"""


def llm_provider() -> str | None:
    """Groq if GROQ_API_KEY is set, otherwise Anthropic if ANTHROPIC_API_KEY is set, otherwise None."""
    if settings.key("groq"):
        return "groq"
    if settings.key("anthropic"):
        return "anthropic"
    return None


GROQ_DEFAULT_MODEL = "openai/gpt-oss-120b"  # llama-3.3-70b-versatile was retired by Groq on 16 Aug 2026


def _ask_groq(prompt: str, model: str, key: str | None = None, max_tokens: int = 2000) -> str:
    """Groq via its OpenAI-compatible chat endpoint (stdlib only, no extra dependency)."""
    import urllib.request
    key = key or settings.key("groq")
    extra = dict(reasoning_effort="low") if "gpt-oss" in model else {}
    body = json.dumps(dict(model=model, temperature=0, max_tokens=max(max_tokens, 1500) if extra else max_tokens, **extra,
                           messages=[dict(role="user", content=prompt)])).encode()
    req = urllib.request.Request(os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": "invoiceflow/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


FILLABLE = ("vendor_name", "invoice_number", "po_number", "total", "subtotal", "tax")
AI_FILL_CONFIDENCE = 0.8


def llm_model() -> str | None:
    p = llm_provider()
    if p == "groq":
        return settings.model() or GROQ_DEFAULT_MODEL
    if p == "anthropic":
        return settings.model() or "claude-sonnet-4-5"
    return None


def ping_llm(provider: str, key: str):
    """Try one tiny request with this key. True = works, False = the key was rejected, None = could not tell (offline)."""
    import urllib.error
    try:
        if provider == "groq":
            _ask_groq('Reply with {"ok":true}', settings.model() or GROQ_DEFAULT_MODEL, key=key)
        else:
            import anthropic
            anthropic.Anthropic(api_key=key).messages.create(model=settings.model() or "claude-sonnet-4-5", max_tokens=8,
                                                             messages=[dict(role="user", content="Reply with ok")])
        return True, ""
    except urllib.error.HTTPError as e:
        return (False if e.code in (401, 403) else None), f"The provider answered HTTP {e.code}."
    except Exception as e:  # noqa: BLE001
        return (False if "authentication" in str(e).lower() or "401" in str(e) else None), str(e)[:140]


def ask_llm(prompt: str, max_tokens: int = 700) -> str:
    """One plain text question to whichever AI reader is configured. Raises if none is set or the call fails."""
    provider = llm_provider()
    if provider is None:
        raise RuntimeError("The AI reader is off. Set GROQ_API_KEY in .env and restart.")
    if provider == "groq":
        return _ask_groq(prompt, settings.model() or GROQ_DEFAULT_MODEL, max_tokens=max_tokens)
    import anthropic
    msg = anthropic.Anthropic(api_key=settings.key("anthropic")).messages.create(
        model=settings.model() or "claude-sonnet-4-5", max_tokens=max_tokens, messages=[dict(role="user", content=prompt)])
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _alnum(x) -> str:
    return re.sub(r"[^a-z0-9]", "", str(x).lower())


def _on_page(name: str, val, text: str) -> bool:
    """The AI may only supply a value that is really written on the page. This is what stops it inventing one."""
    if name in ("total", "subtotal", "tax"):
        try:
            v = float(val)
        except (TypeError, ValueError):
            return False
        return f"{v:,.2f}" in text or f"{v:.2f}" in text
    return bool(_alnum(val)) and _alnum(val) in _alnum(text)


def llm_crosscheck(ex: Extraction, client=None) -> dict:
    """Second reader. Fills a field the rules reader could not read (only from words on the page) and lowers
    confidence where the two readers disagree. Returns {'status', 'agree', 'disagree', 'filled'}. Never raises."""
    provider = "anthropic" if client is not None else llm_provider()
    if provider is None:
        return dict(status="skipped", reason="no LLM key set (GROQ_API_KEY or ANTHROPIC_API_KEY)")
    prompt = LLM_PROMPT.format(text=ex.text[:6000])
    try:
        if provider == "groq":
            model = settings.model() or GROQ_DEFAULT_MODEL
            raw = _ask_groq(prompt, model)
        else:
            if client is None:
                import anthropic
                client = anthropic.Anthropic(api_key=settings.key("anthropic"))
            model = settings.model() or "claude-sonnet-4-5"
            msg = client.messages.create(model=model, max_tokens=600, messages=[dict(role="user", content=prompt)])
            raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception as e:  # noqa: BLE001 - extraction must degrade gracefully
        why = str(e)
        if hasattr(e, "read"):
            try:
                why = f"HTTP {e.code}: {json.loads(e.read())['error']['message']}"
            except Exception:  # noqa: BLE001
                why = f"HTTP {getattr(e, 'code', '?')}"
        return dict(status="error", reason=why[:200])
    agree, disagree, filled, ignored = [], {}, [], {}
    for name in ("vendor_name", "invoice_number", "invoice_date", "po_number", "total", "subtotal", "tax"):
        mine, theirs = ex.get(name), data.get(name)
        if theirs in (None, ""):
            continue   # the AI offered nothing for this field: no opinion, so not a disagreement
        weak = mine is None or ex.conf(name) < config.MIN_FIELD_CONFIDENCE
        if name in FILLABLE and weak and theirs not in (None, "") and _on_page(name, theirs, ex.text):
            val = round(float(theirs), 2) if name in ("total", "subtotal", "tax") else str(theirs).strip()
            ex.fields[name] = FieldValue(val, AI_FILL_CONFIDENCE, "AI reader", "read by the AI reader; the value is written on the page")
            filled.append(name)
            continue
        if mine is None:
            ignored[name] = theirs   # offered a value that is not on the page: not used
            continue
        same = (str(mine).strip().lower() == str(theirs).strip().lower()) if not isinstance(mine, float) \
            else (theirs is not None and abs(float(theirs) - mine) < 0.01)
        if same:
            agree.append(name)
            ex.fields[name].confidence = min(0.99, ex.fields[name].confidence + 0.03)
        else:
            disagree[name] = dict(parser=mine, llm=theirs)
            if name in ex.fields:
                ex.fields[name].confidence = min(ex.fields[name].confidence, 0.5)
                ex.fields[name].note = f"parser and LLM disagree (LLM read {theirs!r})"
    return dict(status="ok", model=model, agree=agree, disagree=disagree, filled=filled, ignored=ignored)


_CARD_SIGNALS = [
    ("approval code", r"approval\s*code"), ("card type", r"card\s*type"), ("entry mode", r"entry\s*mode"),
    ("transaction id", r"transaction\s*id"), ("terminal id", r"terminal\s*id"), ("merchant id", r"merchant\s*id"),
    ("masked card number", r"[x*]{6,}\s*[o0-9]{3,4}"), ("card brand", r"\b(visa|mastercard|discover|amex|american express)\b"),
    ("payment approved", r"response:?\s*approved"), ("swiped or tapped", r"\b(swiped|contactless|chip read)\b"),
]
_INVOICE_WORDS = re.compile(r"\binvoice\b|bill\s*to|purchase\s*order|due\s*date|remit|\bnet\s*\d{2}\b", re.I)


def detect_receipt(text: str) -> list[str]:
    """Names of the card-payment markers found when this looks like a point-of-sale receipt, else an empty list.
    Needs three markers and no invoice wording, so an invoice that merely says 'Visa accepted' is not caught."""
    found = [name for name, rx in _CARD_SIGNALS if re.search(rx, text, re.I)]
    return found if len(found) >= 3 and not _INVOICE_WORDS.search(text) else []


def _self_consistency(ex: Extraction) -> None:
    """When the numbers agree with each other, misreads are very unlikely, so confidence goes up."""
    items = ex.lines
    sub, tax, tot = ex.get("subtotal"), ex.get("tax"), ex.get("total")
    if items and sub is not None and abs(sum(i["amount"] for i in items) - sub) < 0.05:
        for n in ("subtotal",):
            ex.fields[n].confidence = max(ex.fields[n].confidence, 0.99)
            ex.fields[n].note = "line items sum to subtotal"
    if sub is not None and tax is not None and tot is not None and abs(sub + tax - tot) < 0.05:
        ex.fields["total"].confidence = max(ex.fields["total"].confidence, 0.99)
        ex.fields["total"].note = "subtotal + tax = total"


def extract(pdf_bytes: bytes, use_llm: bool = True) -> Extraction:
    text, method, ocr_conf, pages, words = read_pdf(pdf_bytes)
    fields, items = parse_fields(text, method, ocr_conf)
    ex = Extraction(method=method, ocr_confidence=ocr_conf, page_count=pages, text=text, fields=fields, lines=items)
    locate_fields(ex.fields, words)
    _self_consistency(ex)
    if use_llm:
        from . import vision
        why = vision.needs_vision(ex)
        if why and vision.enabled():
            ex.vision = vision.run(pdf_bytes, ex, why)
            _self_consistency(ex)
    ex.llm = llm_crosscheck(ex) if use_llm else dict(status="skipped", reason="disabled")
    return ex
