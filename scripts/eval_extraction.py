"""Field-level accuracy of the reader against the known answers for the sample invoices.

    python -m scripts.eval_extraction            # prints a table and writes data/eval.json

Reports, per field: exact-match accuracy; overall accuracy split by digital vs scanned;
how often the value was located on the page (evidence box); and whether confidence is honest
(mean confidence when right vs when wrong).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.extract import extract  # noqa: E402

FIELDS = ["vendor_name", "tax_id", "bank_last4", "invoice_number", "invoice_date", "po_number", "subtotal", "tax", "total"]


def same(a, b) -> bool:
    if isinstance(b, float):
        return a is not None and abs(float(a) - b) < 0.005
    return str(a) == str(b) if a is not None else b is None


def degrade(pdf_bytes: bytes, seed: int, dpi: int = 130, skew: float = 1.2, noise: float = 14.0) -> bytes:
    """Turn a digital invoice into a noisy, slightly skewed, lower-resolution scan (image-only PDF)."""
    import io, random  # noqa: E401

    import numpy as np
    import pypdfium2 as pdfium
    from PIL import Image
    rnd = random.Random(seed)
    pages = []
    for page in pdfium.PdfDocument(pdf_bytes):
        img = page.render(scale=dpi / 72).to_pil().convert("L")
        img = img.rotate(rnd.uniform(-skew, skew), expand=True, fillcolor=255)
        arr = np.asarray(img, dtype=np.float32) + np.random.default_rng(seed).normal(0, noise, (img.height, img.width))
        pages.append(Image.fromarray(np.clip(arr, 0, 255).astype("uint8")).convert("RGB"))
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:], resolution=dpi)
    return buf.getvalue()


def run(use_llm: bool = False, stress: bool = False) -> dict:
    manifest = json.loads((config.SAMPLES_DIR / "manifest.json").read_text())["samples"]
    manifest = [m for m in manifest if m["category"] not in ("not_invoice", "messy")]
    per_field = {f: dict(n=0, right=0, boxed=0) for f in FIELDS}
    by_method = {"digital": dict(n=0, right=0), "ocr": dict(n=0, right=0)}
    conf_right, conf_wrong, wrong = [], [], []
    for i, s in enumerate(manifest):
        data = (config.SAMPLES_DIR / s["file"]).read_bytes()
        ex = extract(degrade(data, seed=i) if stress else data, use_llm=use_llm)
        for f in FIELDS:
            truth = s["truth"].get(f)
            if truth is None:  # the invoice is meant to lack this field; nothing to score
                continue
            fv = ex.fields.get(f)
            ok = same(ex.get(f), truth)
            per_field[f]["n"] += 1
            per_field[f]["right"] += ok
            per_field[f]["boxed"] += bool(fv and fv.box)
            by_method[ex.method]["n"] += 1
            by_method[ex.method]["right"] += ok
            c = fv.confidence if fv else 0.0
            (conf_right if ok else conf_wrong).append(c)
            if not ok:
                wrong.append(dict(invoice=s["id"], field=f, expected=truth, got=ex.get(f), confidence=round(c, 2)))
    n = sum(v["n"] for v in per_field.values())
    right = sum(v["right"] for v in per_field.values())
    mean = lambda xs: round(sum(xs) / len(xs), 3) if xs else None  # noqa: E731
    return dict(invoices=len(manifest), fields_scored=n, accuracy=round(right / n, 4) if n else 0,
                per_field={f: dict(accuracy=round(v["right"] / v["n"], 3) if v["n"] else None, scored=v["n"],
                                   located_on_page=round(v["boxed"] / v["n"], 3) if v["n"] else None) for f, v in per_field.items()},
                by_method={m: dict(accuracy=round(v["right"] / v["n"], 3) if v["n"] else None, scored=v["n"]) for m, v in by_method.items()},
                mean_confidence_when_right=mean(conf_right), mean_confidence_when_wrong=mean(conf_wrong), wrong=wrong,
                second_reader=use_llm, stress=stress)


def main():
    import os
    llm = bool(os.getenv("GROQ_API_KEY") or os.getenv("ANTHROPIC_API_KEY"))
    clean, stress = run(use_llm=llm), run(use_llm=llm, stress=True)
    (config.DATA_DIR / "eval.json").write_text(json.dumps(dict(clean=clean, stress=stress), indent=1))
    print("== stress: every invoice re-scanned with noise, skew and lower resolution ==")
    print(f"accuracy {stress['accuracy']:.1%}; confidence when right {stress['mean_confidence_when_right']}, when wrong {stress['mean_confidence_when_wrong']}")
    for w in stress["wrong"]:
        print("  miss:", w)
    print("== as generated ==")
    rep = clean
    print(f"{rep['invoices']} invoices, {rep['fields_scored']} fields scored, accuracy {rep['accuracy']:.1%}")
    for m, v in rep["by_method"].items():
        print(f"  {m:8s} {v['accuracy']}  ({v['scored']} fields)")
    print(f"{'field':16s} {'accuracy':>9s} {'on page':>9s}")
    for f, v in rep["per_field"].items():
        print(f"{f:16s} {v['accuracy']!s:>9s} {v['located_on_page']!s:>9s}")
    print("confidence when right:", rep["mean_confidence_when_right"], "| when wrong:", rep["mean_confidence_when_wrong"])
    for w in rep["wrong"]:
        print("  miss:", w)


if __name__ == "__main__":
    main()
