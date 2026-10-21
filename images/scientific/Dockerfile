# Open Terminal sandbox with the Python scientific stack, image processing and office document
# tooling (spreadsheets, PDF, Word, PowerPoint).
#
#   docker build -t ghcr.io/felix920506/open-terminal-scientific:latest images/scientific
#
# Published by .github/workflows/scientific-image.yml.
#
# Builds on the full upstream image (numpy, scipy, pandas, matplotlib, seaborn, plotly,
# scikit-learn, jupyter, openpyxl, xlrd, xlsxwriter, pypdf, python-docx, python-pptx,
# weasyprint, pandoc, poppler-utils, LibreOffice) and adds what it lacks.
# Packages already in the base are pinned to their installed versions so Open Terminal's
# own dependencies are not upgraded underneath it.
ARG BASE_IMAGE=ghcr.io/open-webui/open-terminal:latest
FROM ${BASE_IMAGE}

USER root
# OCR for scanned PDFs (tesseract, with English data), plus the tools ocrmypdf uses
# (ghostscript, qpdf, unpaper, pngquant).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng ghostscript qpdf unpaper pngquant \
    && rm -rf /var/lib/apt/lists/*

RUN pip freeze > /tmp/base-constraints.txt \
    && pip install --no-cache-dir --constraint /tmp/base-constraints.txt \
        # Scientific / numerical
        sympy statsmodels networkx scikit-image numba numexpr bottleneck \
        pyarrow polars xarray netCDF4 h5py tables \
        bokeh altair \
        # Spreadsheets: pandas' optional Excel engines (.xlsx/.xlsm via openpyxl, fast reads
        # via calamine, .xlsb, .ods, legacy .xls), polars' Excel reader (fastexcel), formula
        # evaluation without Excel, password-protected workbooks and table rendering.
        "pandas[excel]" python-calamine pyxlsb odfpy fastexcel \
        formulas msoffcrypto-tool tabulate \
        # PDF: fast text/image extraction and page rendering (PyMuPDF), layout-aware text and
        # tables (pdfplumber), editing/merging/encryption (pikepdf), page images (pdf2image),
        # generation (reportlab, fpdf2), OCR (pytesseract, ocrmypdf) and images to PDF (img2pdf).
        pymupdf pdfplumber pikepdf pdf2image reportlab fpdf2 pytesseract ocrmypdf img2pdf \
        # ocrmypdf needs uharfbuzz; the 0.56.3 wheel and cftime's (netCDF4, xarray) were built
        # with incompatible Cython and cannot be imported in one process. smoke_test.py checks.
        uharfbuzz==0.56.0 \
        # Word: conversion to HTML/Markdown (mammoth), structured extraction (docx2python),
        # templating (docxtpl), merging (docxcompose) and a pandoc wrapper (pypandoc).
        mammoth docx2python docxtpl docxcompose pypandoc \
        # Images (Pillow, ImageMagick and ffmpeg are in the base): OpenCV without GUI libraries,
        # image/video I/O (imageio), scientific TIFF, HEIC/AVIF for Pillow, ImageMagick bindings
        # (Wand) and SVG rasterizing (cairosvg).
        opencv-python-headless imageio imageio-ffmpeg tifffile pillow-heif Wand cairosvg \
        # Office/PDF to Markdown in one call, handy for feeding documents to the model.
        "markitdown[pdf,docx,pptx,xlsx,xls]" \
    && rm /tmp/base-constraints.txt

USER user
