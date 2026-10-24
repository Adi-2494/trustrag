"""
Helpers that make UNTRUSTED text safe to use:
- file names chosen by the uploader
- document text that we place inside prompts
- model output that we show in the browser
"""

import html
import os
import re
import unicodedata

# ---------------------------------------------------------------------------
# File names
# ---------------------------------------------------------------------------
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9 ._()\-]")


def sanitize_filename(name: str, max_length: int = 100) -> str:
    """Keep only plain, harmless characters in an uploaded file's name (no quotes, tags or paths)."""
    name = unicodedata.normalize("NFKC", os.path.basename(name or ""))
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    stem = _UNSAFE_NAME_CHARS.sub("_", stem).strip(" ._") or "document"
    return stem[: max_length - 4] + ".pdf"


# ---------------------------------------------------------------------------
# Text placed inside prompts
# ---------------------------------------------------------------------------
def escape_for_prompt(text: str, attribute: bool = False) -> str:
    """
    Escape <, > and & so untrusted text can never close or fake our <document>/<chunk> tags.
    attribute=True also escapes quotes, for values inside tag attributes like file="...".
    """
    return html.escape(str(text), quote=attribute)


# ---------------------------------------------------------------------------
# Text checked by the rule-based injection scanner
# ---------------------------------------------------------------------------
_ZERO_WIDTH = re.compile("[​-‏‪-‮⁠-⁤﻿]")
_SOFT_HYPHEN = "­"
# Common look-alike letters (Greek/Cyrillic) that attackers swap in for Latin ones.
_LOOKALIKES = str.maketrans({
    "ο": "o", "о": "o", "Ο": "O", "О": "O", "е": "e", "Е": "E", "а": "a", "А": "A",
    "і": "i", "І": "I", "ı": "i", "ѕ": "s", "Ѕ": "S", "р": "p", "Р": "P", "с": "c", "С": "C",
    "у": "y", "х": "x", "Х": "X", "ԁ": "d", "ɡ": "g", "Т": "T", "Н": "H", "М": "M", "К": "K", "В": "B",
    "ν": "v", "α": "a", "ε": "e", "ι": "i", "κ": "k", "τ": "t", "υ": "u",
})
_SPACED_LETTERS = re.compile(r"\b(?:[A-Za-z][\-.*_ ]){3,}[A-Za-z]\b")


def has_hidden_characters(text: str) -> bool:
    """True if the text contains invisible characters often used to hide attacks."""
    return bool(_ZERO_WIDTH.search(text))


def normalise_for_rules(text: str) -> str:
    """Undo common disguises so the regex rules see the real words."""
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", text).replace(_SOFT_HYPHEN, "")
    text = text.translate(_LOOKALIKES)
    text = _SPACED_LETTERS.sub(lambda m: re.sub(r"[\-.*_ ]", "", m.group(0)), text)  # "I-g-n-o-r-e" -> "Ignore"
    text = re.sub(r"\b(you|we|they)['’]re\b", r"\1 are", text, flags=re.I)          # "you're" -> "you are"
    return text


# ---------------------------------------------------------------------------
# Model output shown in the browser
# ---------------------------------------------------------------------------
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_IMAGE_REF = re.compile(r"!\[([^\]]*)\]\[[^\]]*\]")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_LINK_DEF = re.compile(r"^\s*\[[^\]]+\]:\s*\S+.*$", re.M)
_URL = re.compile(r"<?\b((?:https?|ftp)://[^\s<>`]+)>?", re.I)


def safe_markdown(text: str) -> str:
    """
    Make model output safe to render as Markdown:
    images are removed (they load automatically and can leak data to another website),
    links lose their target, and bare URLs are shown as plain, non-clickable code text.
    """
    text = _MD_IMAGE.sub(lambda m: f"[image removed: {m.group(1)}]" if m.group(1) else "[image removed]", text)
    text = _MD_IMAGE_REF.sub("[image removed]", text)
    text = _MD_LINK_DEF.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub(lambda m: f"`{m.group(1)}`", text)
    return text
