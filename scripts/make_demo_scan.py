"""Make a realistic 'phone photo of a printed invoice' for demos: an image-only PDF with a slight tilt, uneven lighting,
sensor noise and JPEG compression. It is a real, payable invoice from Helix Office Solutions for PO-4507, so it should be
approved once read. Usage: python -m scripts.make_demo_scan [out.pdf] [strength 0-3]"""
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageFilter

from scripts.gen_data import build_invoice


def photo_effect(pdf_in: Path, pdf_out: Path, strength: float = 1.0, seed: int = 11):
    rng = np.random.default_rng(seed)
    img = pdfium.PdfDocument(str(pdf_in))[0].render(scale=170 / 72).to_pil().convert("L")
    img = img.rotate(1.1 * strength, resample=Image.BICUBIC, expand=False, fillcolor=235)
    a = np.asarray(img, dtype=np.float32)
    h, w = a.shape
    yy, xx = np.mgrid[0:h, 0:w]
    light = 1.0 - 0.16 * strength * ((xx / w) * 0.7 + (yy / h) * 0.5)          # the corner furthest from the lamp is darker
    a = a * light * 0.92 + 14
    a += rng.normal(0, 6 * strength, a.shape)
    img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.6 + 0.35 * strength))
    tmp = pdf_out.with_suffix(".jpg")
    img.save(tmp, "JPEG", quality=int(70 - 8 * strength))
    Image.open(tmp).convert("L").save(pdf_out, "PDF", resolution=170)
    tmp.unlink()


def main(out: str = "helix_scan.pdf", strength: float = 1.0):
    with tempfile.TemporaryDirectory() as d:
        clean = Path(d) / "clean.pdf"
        build_invoice(clean, vendor_id="V003", template="A", inv_no="INV-HX-3318", inv_date=date.today() - timedelta(days=4),
                      po_ref="PO-4507", po_for_lines="PO-4507")
        photo_effect(clean, Path(out), strength)
    return out


if __name__ == "__main__":
    main(*(sys.argv[1:2] or ["helix_scan.pdf"]), *(float(x) for x in sys.argv[2:3]))
