"""
Step 1 of the pipeline: read a PDF page by page.
Every page keeps its file name and page number (the "metadata"),
so later we can cite answers like [handbook.pdf, p.3].
Pages that are scanned images (no real text) are read with OCR (see ocr.py).
"""

import hashlib
import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from src import config
from src.ocr import ocr_pages


class LoaderError(Exception):
    """A PDF problem with a friendly message we can show in the UI."""


def _read_bytes(file) -> bytes:
    """Accept a file path, raw bytes, or a file-like object (e.g. a Streamlit upload)."""
    if isinstance(file, bytes):
        return file
    if isinstance(file, str):
        with open(file, "rb") as f:
            return f.read()
    if hasattr(file, "getvalue"):
        return file.getvalue()
    return file.read()


def load_pdf(file, file_name: str) -> list[dict]:
    """
    Read a PDF and return one dict per page that has text:
    [{"text": "...", "file_name": "handbook.pdf", "page": 1, "ocr": False}, ...]
    """
    data = _read_bytes(file)
    try:
        reader = PdfReader(io.BytesIO(data))
    except PdfReadError:
        raise LoaderError(f"'{file_name}' could not be read. It may be damaged or not a real PDF.")

    if reader.is_encrypted:
        raise LoaderError(f"'{file_name}' is password-protected. Please upload an unlocked PDF.")

    if len(reader.pages) > config.MAX_PDF_PAGES:
        raise LoaderError(
            f"'{file_name}' has {len(reader.pages)} pages. The limit is {config.MAX_PDF_PAGES} pages per PDF."
        )

    # 1) Try the free way first: read the real text of every page.
    page_texts = {}
    for page_number, page in enumerate(reader.pages, start=1):
        page_texts[page_number] = (page.extract_text() or "").strip()

    # 2) Pages with (almost) no text are probably scanned images -> OCR them.
    scanned = [n for n, text in page_texts.items() if len(text) < config.OCR_MIN_CHARS]
    if len(scanned) > config.OCR_MAX_PAGES:
        raise LoaderError(
            f"'{file_name}' has {len(scanned)} scanned pages, more than the OCR limit of "
            f"{config.OCR_MAX_PAGES}. Increase OCR_MAX_PAGES in src/config.py if you want to process it."
        )
    ocr_texts = {}
    if scanned:
        file_hash = hashlib.sha256(data).hexdigest()
        ocr_texts = ocr_pages(data, scanned, file_hash)

    # 3) Build the final list of pages (skip pages that are still empty).
    pages = []
    for page_number, text in page_texts.items():
        used_ocr = page_number in ocr_texts and len(ocr_texts[page_number].strip()) > len(text)
        if used_ocr:
            text = ocr_texts[page_number].strip()
        if text:
            pages.append({"text": text, "file_name": file_name, "page": page_number, "ocr": used_ocr})

    if not pages:
        raise LoaderError(f"No readable text found in '{file_name}', even with OCR.")
    return pages
