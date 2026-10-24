"""
Step 2 of the pipeline: split each page into small, overlapping chunks that keep their file name and page.

Two strategies (compared by the evaluation suite):
- fixed:   split the page text by size, preferring paragraph and sentence boundaries;
- section: split the page at its headings first, chunk each section, and prefix every chunk with its heading,
           so a chunk never loses the context that says what it is about.
"""

import re

from langchain_text_splitters import RecursiveCharacterTextSplitter

from src import config
from src.models import Chunk, Page

_TRAILING_PUNCTUATION = (".", ",", ";", ":", "!", "?")


def is_heading(line: str) -> bool:
    """A short line, starting with a capital or digit, without sentence punctuation at the end."""
    line = line.strip()
    return (
        3 <= len(line) <= 80
        and len(line.split()) <= 10
        and not line.endswith(_TRAILING_PUNCTUATION)
        and bool(re.match(r"[A-Z0-9]", line))
    )


def split_sections(text: str) -> list[tuple[str, str]]:
    """[(heading, body), ...]; text before the first heading gets an empty heading."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.split("\n"):
        if is_heading(line):
            sections.append((line.strip(), []))
        else:
            sections[-1][1].append(line)
    return [(heading, "\n".join(body).strip()) for heading, body in sections if heading or "".join(body).strip()]


def _make_splitter(chunk_size: int, chunk_overlap: int) -> RecursiveCharacterTextSplitter:
    # Prefer paragraph, then line, then sentence boundaries, so chunks rarely end mid-sentence.
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
        keep_separator="end",  # keep the full stop with its sentence, not at the start of the next chunk
    )


_default_splitter = _make_splitter(config.CHUNK_SIZE, config.CHUNK_OVERLAP)


def chunk_pages(
    pages: list[Page], chunk_size: int | None = None, chunk_overlap: int | None = None, strategy: str | None = None
) -> list[Chunk]:
    """
    Turn pages into chunks:
    [{"id": "handbook.pdf-p2-c0", "text": "...", "file_name": "handbook.pdf", "page": 2}, ...]
    chunk_size / chunk_overlap / strategy override the configured values (used by the evaluation ablations).
    """
    strategy = strategy or config.CHUNK_STRATEGY
    if chunk_size is None and chunk_overlap is None:
        splitter = _default_splitter
    else:
        splitter = _make_splitter(
            chunk_size or config.CHUNK_SIZE, config.CHUNK_OVERLAP if chunk_overlap is None else chunk_overlap
        )
    chunks: list[Chunk] = []
    for page in pages:
        if strategy == "section":
            pieces = [
                f"{heading}\n{piece}" if heading else piece
                for heading, body in split_sections(page["text"])
                for piece in splitter.split_text(body)
            ]
        else:
            pieces = splitter.split_text(page["text"])
        for i, piece in enumerate(pieces):
            chunks.append(
                {
                    # Stable IDs: re-uploading the same file replaces its chunks instead of duplicating them.
                    "id": f"{page['file_name']}-p{page['page']}-c{i}",
                    "text": piece,
                    "file_name": page["file_name"],
                    "page": page["page"],
                }
            )
    return chunks
