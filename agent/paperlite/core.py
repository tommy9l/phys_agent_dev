"""
Shared plumbing: the PyMuPDF import, tunables, regexes, and small geometry /
font helpers that every other module uses. Nothing in here touches a page.
"""
from __future__ import annotations

import re
import sys

try:
    import pymupdf  # PyMuPDF >= 1.24.3
except ImportError:  # older releases only ship the legacy module name
    try:
        import fitz as pymupdf  # type: ignore[no-redef]
    except ImportError:
        sys.exit("PyMuPDF is required:  python3.11 -m pip install pymupdf")

Rect = pymupdf.Rect

# --------------------------------------------------------------------------- #
# Tunables                                                                    #
# --------------------------------------------------------------------------- #
# Let MuPDF expand ligatures (fi -> "fi") and compute tighter glyph bboxes.
TEXT_FLAGS = pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES
TEXT_FLAGS |= getattr(pymupdf, "TEXT_ACCURATE_BBOXES", 0)


# --------------------------------------------------------------------------- #
# Regexes                                                                     #
# --------------------------------------------------------------------------- #
# Where the paper proper starts. Handles "Abstract", "ABSTRACT", Elsevier's
# letter-spaced "a b s t r a c t", "Summary", and "1 Introduction" / "I. INTRODUCTION".
# Caption label: "Figure 3:", "Fig. 2.", "TABLE IV", "Table S1 |". Roman numerals are
# matched case-sensitively, otherwise "Tab-l-e" in "TableFormer" reads as Tab + L.
CAPTION_RE = re.compile(
    r"^((?:fig(?:ure)?|table|tab|algorithm|listing)\b\.?\s*(?:S?\d+|(?-i:[IVXL]+))[a-z]?\b)"
    r"\s*([.:|—–-])?",
    re.I,
)
TABLE_CAPTION_RE = re.compile(r"^tab", re.I)
# "[12] A. Author", "3. ", "• " -- but not an in-text citation that happens to start a
# line ("[1], f_B = ...", "[8]. Experimental", "[7] and")
PAGE_NUM_RE = re.compile(
    r"^(?:page\s*)?[–—-]?\s*\d{1,4}\s*[–—-]?(?:\s*(?:of|/)\s*\d{1,4})?$", re.I
)

# Font-name heuristics (subset prefix "ABCDEF+" is stripped first).
MATH_FONT_RE = re.compile(
    r"(?i:cmmi|cmsy|cmex|cmbsy|msam|msbm|eufm|eusm|euex|rsfs|stmary|wasy|lm(?:math|sy|ex)"
    r"|txmi|txsy|txex|pxmi|pxsy|mtmi|mtsy|mtex|math)"
)
EXT_FONT_RE = re.compile(r"(?i:cmex|lmex|txex|euex|mtex|largesymbols|extension)")
BOLD_FONT_RE = re.compile(r"(?i:bold|black|heavy|semibold|demi|medi(?:um)?\b|cmbx|sfbx)|TBI?$")
ITALIC_FONT_RE = re.compile(r"(?i:italic|oblique|ital\b|cmti|sfti)|-It$|TBI$|TI$")
MONO_FONT_RE = re.compile(
    r"(?i:mono|courier|consola|cmtt|sftt|typewriter|menlo|inconsolata|sourcecode|lmmono)"
)

TEXT_FIXES = str.maketrans({
    " ": " ", "­": "-", "​": "", "‐": "-", "‑": "-",
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
})


# --------------------------------------------------------------------------- #
# Geometry                                                                    #
# --------------------------------------------------------------------------- #


def inside_frac(inner: Rect, outer: Rect) -> float:
    """Fraction of inner's area that lies in outer (0 for degenerate rects)."""
    a = inner.get_area()
    if a <= 0:
        return 0.0
    x = inner & outer
    return 0.0 if x.is_empty else x.get_area() / a


def union_rects(rects) -> Rect:
    """Union of an iterable of rects (avoids PyMuPDF's empty-rect edge cases)."""
    it = iter(rects)
    out = Rect(next(it))
    for r in it:
        out |= r
    return out


# --------------------------------------------------------------------------- #
# Text                                                                        #
# --------------------------------------------------------------------------- #
def clean_text(text: str) -> str:
    """Undo ligatures, soft hyphens, NBSPs and other PDF typesetting artefacts."""
    return text.translate(TEXT_FIXES)


def looks_like_prose(text: str, min_words: int = 20) -> bool:
    """
    Sentences are full of lowercase words ("the", "we", "of"); author lists and
    affiliations are mostly Capitalised Names. That one ratio separates them well.
    """
    words = re.findall(r"[A-Za-z]{2,}", text)
    if len(words) < min_words:
        return False
    return sum(w[0].islower() for w in words) / len(words) >= 0.45


def caption_label(text: str, first_line: str = "") -> str | None:
    """
    Return the caption label ("Figure 3:") if the text starts a caption.
    "Table 1 shows ..." is a sentence, not a caption, so a bare label only counts
    when it is punctuated, ALL CAPS (IEEE "TABLE II"), or alone on its line.
    """
    m = CAPTION_RE.match(text)
    if not m:
        return None
    alone = first_line.strip() == m.group(1).strip()
    if m.group(2) or m.group(1).split()[0].isupper() or alone:
        return m.group(0)
    return None


def join_hyphenated(left: str, right: str) -> str:
    """
    Join 'left-' + 'right' across a line break. Syllable breaks lose the hyphen
    ("excep-" + "tionally"), but CamelCase/acronym compounds keep it
    ("DocLayNet-" + "trained").
    """
    word = re.search(r"(\S+)-$", left)
    w = word.group(1) if word else ""
    if w and (len(w) == 1 or "-" in w or re.search(r"[A-Z0-9]", w[1:])):
        return left + right
    return left[:-1] + right


# --------------------------------------------------------------------------- #
# Fonts                                                                       #
# --------------------------------------------------------------------------- #
def base_font(name: str) -> str:
    """'ABCDEF+CMMI10' -> 'CMMI10' (strip the font-subset tag)."""
    return name.split("+", 1)[-1]


def span_bold(s: dict) -> bool:
    return bool(s["flags"] & 16) or bool(BOLD_FONT_RE.search(base_font(s["font"])))


def span_italic(s: dict) -> bool:
    return bool(s["flags"] & 2) or bool(ITALIC_FONT_RE.search(base_font(s["font"])))


def span_mono(s: dict) -> bool:
    return bool(s["flags"] & 8) or bool(MONO_FONT_RE.search(base_font(s["font"])))


def is_roman(font: str) -> bool:
    """Upright text font (CMR, Times...), as opposed to a math-italic/symbol font."""
    return not MATH_FONT_RE.search(base_font(font))


