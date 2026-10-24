"""
Step 2 of the pipeline: split each page into small, overlapping chunks.
Each chunk keeps the file name and page number from its page.
"""

from langchain_text_splitters import RecursiveCharacterTextSplitter

from src import config

# The splitter tries to cut at paragraph breaks first, then lines, then sentences,
# then words - so chunks rarely end in the middle of a sentence.
_splitter = RecursiveCharacterTextSplitter(
    chunk_size=config.CHUNK_SIZE,
    chunk_overlap=config.CHUNK_OVERLAP,
    separators=["\n\n", "\n", ". ", " ", ""],
    keep_separator="end",  # keep the full stop at the end of a sentence, not the start of the next chunk
)


def chunk_pages(pages: list[dict]) -> list[dict]:
    """
    Turn pages into chunks:
    [{"id": "handbook.pdf-p2-c0", "text": "...", "file_name": "handbook.pdf", "page": 2}, ...]
    """
    chunks = []
    for page in pages:
        pieces = _splitter.split_text(page["text"])
        for i, piece in enumerate(pieces):
            chunks.append({
                # A stable, unique ID: the same file always produces the same IDs,
                # so re-uploading a file replaces its chunks instead of duplicating them.
                "id": f"{page['file_name']}-p{page['page']}-c{i}",
                "text": piece,
                "file_name": page["file_name"],
                "page": page["page"],
            })
    return chunks
