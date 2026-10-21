"""Smoke test for the scientific image: every added library imports and does real work.

Run inside the image:  python3 smoke_test.py
"""

import os
import tempfile

T = tempfile.mkdtemp()


def path(name: str) -> str:
    return os.path.join(T, name)


# ── scientific ──────────────────────────────────────────────────────────────
# ── spreadsheets ────────────────────────────────────────────────────────────
import formulas  # noqa: E402,F401
import h5py  # noqa: E402,F401
import numpy  # noqa: E402,F401
import pandas as pd  # noqa: E402
import polars  # noqa: E402
import scipy  # noqa: E402,F401
import sklearn  # noqa: E402,F401
import statsmodels  # noqa: E402,F401
import sympy  # noqa: E402,F401
import xarray  # noqa: E402,F401

df = pd.DataFrame({"a": [1, 2, 3]})
for name, engine in [("t.xlsx", "xlsxwriter"), ("t.ods", "odf")]:
    df.to_excel(path(name), index=False, engine=engine)
    assert pd.read_excel(path(name)).equals(df), name
assert pd.read_excel(path("t.xlsx"), engine="calamine").equals(df)
assert polars.read_excel(path("t.xlsx")).shape == (3, 1)

# ── PDF ─────────────────────────────────────────────────────────────────────
import img2pdf  # noqa: E402
import pdfplumber  # noqa: E402
import pikepdf  # noqa: E402
import pymupdf  # noqa: E402
import pypdf  # noqa: E402
from fpdf import FPDF  # noqa: E402
from pdf2image import convert_from_path  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

c = canvas.Canvas(path("r.pdf"))
c.drawString(72, 720, "Hello from reportlab")
c.save()
assert "Hello from reportlab" in pymupdf.open(path("r.pdf"))[0].get_text()
with pdfplumber.open(path("r.pdf")) as pdf:
    assert "Hello from reportlab" in pdf.pages[0].extract_text()
assert "Hello from reportlab" in pypdf.PdfReader(path("r.pdf")).pages[0].extract_text()

f = FPDF()
f.add_page()
f.set_font("helvetica", size=12)
f.cell(text="Hello from fpdf2")
f.output(path("f.pdf"))
with pikepdf.open(path("r.pdf")) as merged, pikepdf.open(path("f.pdf")) as other:
    merged.pages.extend(other.pages)
    merged.save(path("merged.pdf"))
assert len(pypdf.PdfReader(path("merged.pdf")).pages) == 2
(page,) = convert_from_path(path("r.pdf"), dpi=50)
assert page.size[0] > 0

# ── OCR ─────────────────────────────────────────────────────────────────────
import pytesseract  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

img = Image.new("RGB", (900, 160), "white")
ImageDraw.Draw(img).text((20, 40), "OCR WORKS 42", fill="black", font=ImageFont.load_default(size=64))
img.save(path("ocr.png"))
assert "OCR WORKS 42" in pytesseract.image_to_string(img), pytesseract.image_to_string(img)
with open(path("scan.pdf"), "wb") as fh:
    fh.write(img2pdf.convert(path("ocr.png")))
import ocrmypdf  # noqa: E402

ocrmypdf.ocr(path("scan.pdf"), path("scan-ocr.pdf"), progress_bar=False)
assert "OCR WORKS 42" in pymupdf.open(path("scan-ocr.pdf"))[0].get_text()

# ── Word ────────────────────────────────────────────────────────────────────
import docx  # noqa: E402
import mammoth  # noqa: E402
import pypandoc  # noqa: E402
from docx2python import docx2python  # noqa: E402
from docxcompose.composer import Composer  # noqa: E402
from docxtpl import DocxTemplate  # noqa: E402

d = docx.Document()
d.add_paragraph("Hello {{ name }}")
d.save(path("tpl.docx"))
tpl = DocxTemplate(path("tpl.docx"))
tpl.render({"name": "Word"})
tpl.save(path("w.docx"))
with open(path("w.docx"), "rb") as fh:
    assert "Hello Word" in mammoth.convert_to_markdown(fh).value
with docx2python(path("w.docx")) as content:
    assert "Hello Word" in content.text
composer = Composer(docx.Document(path("w.docx")))
composer.append(docx.Document(path("w.docx")))
composer.save(path("both.docx"))
assert pypandoc.convert_file(path("both.docx"), "plain").count("Hello Word") == 2

# ── PowerPoint ──────────────────────────────────────────────────────────────
import pptx  # noqa: E402

prs = pptx.Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[1])
slide.shapes.title.text = "Hello PowerPoint"
prs.save(path("p.pptx"))

# ── everything to Markdown ──────────────────────────────────────────────────
from markitdown import MarkItDown  # noqa: E402

md = MarkItDown()
assert "Hello PowerPoint" in md.convert(path("p.pptx")).text_content
assert "Hello Word" in md.convert(path("w.docx")).text_content
assert "Hello from reportlab" in md.convert(path("r.pdf")).text_content
assert "a" in md.convert(path("t.xlsx")).text_content

# ── images ──────────────────────────────────────────────────────────────────
import cairosvg  # noqa: E402
import cv2  # noqa: E402
import imageio.v3 as iio  # noqa: E402
import pillow_heif  # noqa: E402
import skimage  # noqa: E402,F401
import tifffile  # noqa: E402
from wand.image import Image as WandImage  # noqa: E402

gray = cv2.cvtColor(cv2.imread(path("ocr.png")), cv2.COLOR_BGR2GRAY)
assert cv2.Canny(gray, 50, 150).any()
arr = iio.imread(path("ocr.png"))
tifffile.imwrite(path("a.tif"), arr)
assert (tifffile.imread(path("a.tif")) == arr).all()
iio.imwrite(path("v.mp4"), numpy.zeros((5, 64, 64, 3), dtype=numpy.uint8), fps=5)
assert iio.imread(path("v.mp4")).shape[0] == 5
pillow_heif.register_heif_opener()
img.save(path("a.heic"))
assert Image.open(path("a.heic")).size == img.size
with WandImage(filename=path("ocr.png")) as w:
    w.resize(90, 16)
    assert w.size == (90, 16)
cairosvg.svg2png(
    bytestring=b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>',
    write_to=path("s.png"),
)
assert Image.open(path("s.png")).size == (10, 10)

print("ok")
