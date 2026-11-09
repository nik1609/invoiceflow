"""Reset the database. It starts with no runs at all, so the dashboard only ever shows work that was really done.

The only starting data is an opening PO ledger: invoices billed against some POs before this tool was used, so that
the split-PO examples have a real balance to work from."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, db, master  # noqa: E402


def seed():
    db.reset_all()
    ledger = json.loads((config.DATA_DIR / "ledger_seed.json").read_text())
    for row in ledger:
        db.add_ledger(row["po"], row["vendor"], row["inv"], "".join(ch for ch in row["inv"].upper() if ch.isalnum()), row["amount"],
                      row["date"], " ".join(l[0] for l in row["lines"]), "committed", "opening", source="opening")


if __name__ == "__main__":
    seed()
    print("Database reset:", config.DB_PATH)
