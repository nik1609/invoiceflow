"""Central configuration. Every business threshold lives here so it can be explained and tuned in one place."""
import os
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv():
    """Read KEY=value lines from .env in the project folder. A variable already set in the terminal wins."""
    f = BASE_DIR / ".env"
    if os.getenv("IF_NO_DOTENV") or not f.is_file():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip("\"'"))


_load_dotenv()
DATA_DIR = Path(os.getenv("IF_DATA_DIR", BASE_DIR / "data"))
SAMPLES_DIR = DATA_DIR / "samples"
MASTER_XLSX = DATA_DIR / "procurement.xlsx"
DB_PATH = Path(os.getenv("IF_DB_PATH", DATA_DIR / "invoiceflow.db"))
UPLOAD_DIR = DATA_DIR / "uploads"

# Visual pacing for the live run view. Set to 0 in tests.
STAGE_DELAY = float(os.getenv("IF_STAGE_DELAY", "0.8"))

# Reference date for date sanity checks. Overridable so tests are deterministic.
def today() -> date:
    v = os.getenv("IF_TODAY")
    return date.fromisoformat(v) if v else date.today()


# ---- Business rules -------------------------------------------------------
# Cumulative billing against a PO, relative to the PO gross total.
AUTO_TOLERANCE_PCT = 0.02      # up to 2% over: approve with a note
AUTO_TOLERANCE_ABS = 500.00    # ...and never more than this many currency units
REVIEW_TOLERANCE_PCT = 0.10    # 2% to 10% over: hold for a human; above 10%: reject and dispute

MATH_TOLERANCE_ABS = 0.05      # rounding slack when checking line items + tax = total
LINE_PRICE_TOLERANCE_PCT = 0.02

DUP_WINDOW_DAYS = 14           # near-duplicate window for same vendor and same amount
NAME_VARIANT_THRESHOLD = 80    # fuzzy name score that counts as "looks like our vendor"
NAME_RESOLVE_THRESHOLD = 92    # fuzzy name score accepted when the invoice carries no tax id

IMPLIED_PO_APPROVE_CONF = 0.90  # inferred PO is accepted (with a note) at or above this
IMPLIED_PO_MIN_MARGIN = 0.15    # best candidate must beat the runner-up by this much
VISION_MIN_OCR = 0.85           # a scan below this average OCR confidence also goes to the vision model
MIN_FIELD_CONFIDENCE = 0.70     # below this on a critical field, a human looks at it

CRITICAL_FIELDS = ("invoice_number", "invoice_date", "total", "vendor_name")
