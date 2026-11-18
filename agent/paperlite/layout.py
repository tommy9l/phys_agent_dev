#!/usr/bin/env python3
"""Page layout and output for paperlite (see __init__.py for usage).

Layout comes from generic rules, nothing tuned to a journal template:
  * reading order (one or two columns, gutter and line spacing measured per paper);
  * headings, paragraphs, captions, contents lists, references, page markers;
  * anything two-dimensional is cropped to a PNG and linked where it sits:
      figure   - the graphics between its caption and the text above it,
      table    - its caption plus everything down to the next paragraph,
      equation - the lines around a "(n)" number (unnumbered display math stays text);
  * long author lists become one line (--keep-authors keeps them); the collaboration line stays.

The text of each line comes from the glyph engine (textblocks + inline: stacked scripts, TeX sums
and brackets, vector accents, overlines) and tables from tablekit. Their LaTeX is rewritten here
into one plain notation: Unicode symbols, _{..} / ^{..}, units as words.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import unicodedata
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

# Inside the analysis repo, PDFs and their conversions both live in data/papers/ (gitignored):
# data/papers/<stem>.pdf -> data/papers/<stem>/<stem>.md, index.json, images/, tables/, equations/
PAPER_DIR = Path(__file__).resolve().parents[2] / "data" / "papers"

TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP
CAPTION_RE = re.compile(r"^(?P<label>(?P<kind>fig(?:ure)?|table)\.?\s*(?P<num>[A-Z]?\d+|[IVX]+)(?:\s*[.:|—–]|\s*$))", re.I)
TAG_RE = re.compile(r"\(\s*((?:[A-Z]\.?)?\d+(?:\.\d+)?[a-z]?)\s*\)")
SECNUM_RE = re.compile(r"^(?:(?P<d>\d+(?:\.\d+)*)\.?|(?P<r>[IVX]+)\.|(?P<a>[A-H])\.)\s+\S")
LIST_RE = re.compile(r"^(?:\[\d{1,3}\]|\d{1,3}\.\s|[•◦▪∙–-]\s|\([a-z0-9]{1,3}\)\s)")
LONG_WORD_RE = re.compile(r"(?<![\w^_{])(?!(?:exp|sin|cos|tan|log|max|min|det|arg|sup|inf|lim)\b)[A-Za-z][a-z]{2,}\b")
SCRIPT_RE = re.compile(r"[\^_]\{[^{}]*\}")
DOTS_RE = re.compile(r"(?:\s*\.){4,}\s*")
AFFIL_RE = re.compile(r"Universi|Institut|Laborator|Department|Dipartimento|Departamento|Faculty|"
                      r"College|School|Academy|Center|Centre|Research|INFN|Sezione|National|Hochschule", re.I)
PAGE_NUM_RE = re.compile(r"^[\s\-–—]*(?:page\s*)?(?:\d+|[ivxlc]+)(?:\s*(?:of|/)\s*\d+)?[\s\-–—]*$", re.I)
HOLD = "\u2063"         # invisible placeholder for a glyph PyMuPDF could not map to Unicode
PITCH = 1.2            # body line spacing / font size, measured per document in load()
MATH_CHARS = set("=<>≤≥≈∼≃∝±∓×·∑∏∫√∂∇→←↔⇒∞∈⊂≡≠|")
UNNUMBERED_SECTIONS = {"abstract", "acknowledgments", "acknowledgements", "acknowledgment", "references",
                       "bibliography", "appendix", "contents", "conclusion", "conclusions", "introduction"}
PARTICLES = {"van", "von", "de", "der", "den", "del", "della", "di", "da", "dos", "du", "la", "le", "ten", "ter", "y"}
# TeX puts an accent glyph before its base letter: "¯ν" is ν-bar, "⃗p" is p-vector.
SPACING_ACCENTS = {"¯": "̄", "ˆ": "̂", "˜": "̃", "˙": "̇", "¨": "̈",
                   "´": "́", "`": "̀", "ˇ": "̌", "˘": "̆", "˚": "̊"}
ACCENT_RE = re.compile("([" + "".join(SPACING_ACCENTS) + "])([^\\W_])")       # "¨u" -> "ü": always
COMBINING_RE = re.compile("(?<![^\\W_])([\u0300-\u036f\u20d0-\u20ff])([^\\W_])")  # " ⃗p" -> " p⃗": only when loose


# ----------------------------------------------------------------------------- text lines

@dataclass
class Span:
    x0: float
    y0: float
    x1: float
    y1: float
    base: float          # baseline y
    size: float
    text: str
    bold: bool
    sup: bool
    italic: bool = False


@dataclass
class Line:
    spans: list
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    size: float = 0.0
    base: float = 0.0
    text: str = ""
    bold: bool = False
    italic: bool = False
    gaps: int = 0        # gaps wider than 1.5 em between spans (table cells, TOC numbers)
    tag: str = ""        # trailing equation number, e.g. "7" for "(7)"
    col: str = "F"       # L / R column, or F (spans the gutter / single column)
    cx0: float = 0.0     # column bounds
    cx1: float = 0.0
    kind: str = ""       # title | head | cap | text | "" (unclassified)
    level: int = 0
    claimed: bool = False
    words: int = 0

    @property
    def rect(self):
        return pymupdf.Rect(self.x0, self.y0, self.x1, self.y1)


def clean(t: str) -> str:
    t = t.replace("\u00a0", " ").replace("\ufffd", "")      # U+FFFD: glyph without Unicode
    return "".join(c for c in t if c in "\t\n" or unicodedata.category(c) not in ("Cc", "Co", "Cs"))


def fix_accents(t: str) -> str:
    """Put each accent after the letter it sits on. TeX writes the accent first ("¨u", "⃗p");
    a combining mark that already follows a letter ("ü") is left alone."""
    t = ACCENT_RE.sub(lambda m: m.group(2) + SPACING_ACCENTS[m.group(1)], t)
    t = COMBINING_RE.sub(lambda m: m.group(2) + m.group(1), t)
    return unicodedata.normalize("NFC", t)


def raw_lines(page) -> list[list[Span]]:
    out = []
    for b in page.get_text("rawdict", flags=TEXT_FLAGS)["blocks"]:
        for ln in b.get("lines", ()):
            if abs(ln["dir"][0] - 1) > 1e-3:          # rotated text: arXiv stamp, axis titles
                continue
            spans = []
            for s in ln["spans"]:
                chars = s["chars"]
                raw = "".join(c["c"] for c in chars)
                t = clean(raw)
                if not t.strip():
                    if "\ufffd" not in raw:
                        continue
                    t = HOLD                                  # unmapped glyph (a TeX ∑): keep its place
                solid = [c for c in chars if c["c"].strip() and not unicodedata.combining(c["c"])]
                if solid and solid[0] is not chars[0]:   # a leading combining accent: start at its letter
                    s["bbox"] = (solid[0]["bbox"][0], *s["bbox"][1:])
                bold = bool(s["flags"] & 16) or bool(re.search(r"bold|black|heavy|cmbx|-medi|semibd", s["font"], re.I))
                italic = bool(s["flags"] & 2) or bool(re.search(r"italic|oblique|-it\b|cmti", s["font"], re.I))
                spans.append(Span(*s["bbox"], s["origin"][1], s["size"], t, bold, bool(s["flags"] & 1), italic))
            if spans:
                out.append(spans)
    return out


def main_span(spans):
    big = max(s.size for s in spans)
    return max((s for s in spans if s.size >= 0.85 * big), key=lambda s: len(s.text.strip()))


def role(s: Span, size: float, base: float) -> str:
    if is_accent(s):
        return ""
    if s.size <= 0.88 * size or s.sup:
        if s.base < base - 0.18 * size:
            return "^"
        if s.base > base + 0.08 * size:
            return "_"
    elif s.base < base - 0.35 * size and not set(s.text.strip()) <= set("√∑∏∫()[]{}|‖⟨⟩"):
        return "^"                                       # raised, same size (but not a big radical/bracket)
    return ""


def render(spans, size, base, cell_gap=None) -> str:
    """Spans (sorted by x) -> text with _{..}/^{..}; cell_gap splits cells with ' | '."""
    parts: list[list] = []                            # [role, text]
    prev, accent_at = None, None                      # accents don't count for spacing; their base does
    for s in spans:
        r = role(s, size, base)
        if not r and s.size <= 0.75 * size and parts and parts[-1][0] and not is_accent(s):
            r = parts[-1][0]                             # script of a script (e^{+} inside a subscript)
        t = s.text if not r else s.text.strip()
        if prev is not None and not is_accent(s):
            gap = s.x0 - prev.x1
            at = accent_at if accent_at is not None else len(parts)
            before = parts[at - 1][1] if at else ""
            if cell_gap and gap > cell_gap:
                parts.insert(at, ["", " | "])
            elif gap > 0.2 * size and not before.endswith(" ") and not t.startswith(" "):
                parts.insert(at, ["", " "])
        if is_accent(s):
            accent_at = len(parts)
        else:
            prev, accent_at = s, None
        if parts and parts[-1][0] == r and r:
            parts[-1][1] += t
        else:
            parts.append([r, t])
    out, i = [], 0
    while i < len(parts):                                # a run of scripts = one ^{..} and/or one _{..}
        if not parts[i][0]:                              # (stacked errors: 8.8^{+8.5}_{-8.2})
            out.append(parts[i][1])
            i += 1
            continue
        got: dict[str, str] = {}
        while i < len(parts) and parts[i][0]:
            got[parts[i][0]] = got.get(parts[i][0], "") + parts[i][1].strip()
            i += 1
        for r, t in got.items():
            if not t.replace(HOLD, ""):
                continue
            if t in ("′", "″"):
                out.append(t)
            elif r == "^" and t in ("◦", "∘", "o"):
                out.append("°")
            else:
                out.append(f"{r}{{{t}}}")
    return fix_accents(re.sub(r"[ \t]+", " ", "".join(out).replace(HOLD, "")).strip())


def is_accent(s: Span) -> bool:
    return all(unicodedata.combining(c) or c in SPACING_ACCENTS or c == "⃗" for c in s.text.strip())


def make_line(spans) -> Line:
    def key(s):
        if is_accent(s):                                 # TeX order: accent, then its base letter
            c = (s.x0 + s.x1) / 2
            under = [o for o in spans if o is not s and not is_accent(o) and o.x0 - 0.5 <= c <= o.x1 + 0.5]
            if under:
                return min(o.x0 for o in under) - 0.01
        return s.x0
    spans = sorted(spans, key=key)
    m = main_span(spans)
    ln = Line(spans, min(s.x0 for s in spans), min(s.y0 for s in spans), max(s.x1 for s in spans),
              max(s.y1 for s in spans), m.size, m.base)
    ln.text = render(spans, ln.size, ln.base)
    chars = sum(len(s.text.strip()) for s in spans) or 1
    ln.bold = sum(len(s.text.strip()) for s in spans if s.bold) / chars >= 0.6
    ln.italic = sum(len(s.text.strip()) for s in spans if s.italic) / chars >= 0.8
    cuts = [i for i in range(1, len(spans)) if spans[i].x0 - spans[i - 1].x1 > 1.5 * ln.size]
    ln.gaps = len(cuts)
    big = [i for i in range(1, len(spans)) if spans[i].x0 - spans[i - 1].x1 > 1.0 * ln.size]
    tail = render(spans[big[-1]:], ln.size, ln.base) if big else ln.text
    if TAG_RE.fullmatch(tail.strip()):
        ln.tag = TAG_RE.fullmatch(tail.strip()).group(1)
    ln.words = len(LONG_WORD_RE.findall(SCRIPT_RE.sub(" ", ln.text)))
    return ln


def visual_lines(raws, gutter, two_col) -> list[Line]:
    """Merge PyMuPDF lines that share a baseline (script fragments, split runs)."""
    items = []
    for spans in raws:
        m = main_span(spans)
        items.append((min(s.x0 for s in spans), min(s.y0 for s in spans), max(s.x1 for s in spans),
                      max(s.y1 for s in spans), m.size, m.base))
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order = sorted(range(len(items)), key=lambda i: items[i][1])
    for k, i in enumerate(order):
        a = items[i]
        for j in order[k + 1:]:
            b = items[j]
            if b[1] > a[3]:
                break
            big, small = (a, b) if a[4] >= b[4] else (b, a)
            if small[4] > 0.85 * big[4]:
                same = abs(a[5] - b[5]) < 0.3 * big[4]
            else:
                same = big[1] <= (small[1] + small[3]) / 2 <= big[3]
            if not same or max(a[0], b[0]) - min(a[2], b[2]) > 2.5 * big[4]:
                continue
            if two_col and ((a[2] <= gutter and b[0] >= gutter) or (b[2] <= gutter and a[0] >= gutter)):
                continue
            parent[find(i)] = find(j)
    groups: dict[int, list] = {}
    for i, spans in enumerate(raws):
        groups.setdefault(find(i), []).extend(spans)
    return [make_line(g) for g in groups.values()]


# ----------------------------------------------------------------------------- document model

def mode(vals, default):
    vals = [round(v) for v in vals]
    return Counter(vals).most_common(1)[0][0] if vals else default


def find_gutter(raws, width, body):
    """Emptiest vertical strip in the middle of the page, judged from column-wide text lines.
    Returns (gutter x, two_col, crossing) where crossing[x] counts text lines of any width over x."""
    cover = [0] * (int(width) + 2)
    crossing = [0] * (int(width) + 2)
    sides = [0, 0]
    for spans in raws:
        x0, x1 = min(s.x0 for s in spans), max(s.x1 for s in spans)
        if sum(len(s.text) for s in spans) < 12 or main_span(spans).size < 0.75 * body:
            continue                                   # figure labels say nothing about columns
        for x in range(max(0, int(x0)), min(len(cover), int(x1) + 1)):
            crossing[x] += 1
        if not 0.25 * width <= x1 - x0 <= 0.6 * width:
            continue                                   # table cells, full-width lines
        for x in range(max(0, int(x0)), min(len(cover), int(x1) + 1)):
            cover[x] += 1
        sides[0] += x1 < width / 2 + 20
        sides[1] += x0 > width / 2 - 20
    lo, hi = int(width * 0.38), int(width * 0.62)
    best = min(range(lo, hi), key=lambda x: cover[x])
    run = [x for x in range(lo, hi) if cover[x] == cover[best]]
    gutter = (run[0] + run[-1]) / 2 if run else width / 2
    two_col = cover[best] <= 0.12 * max(cover) and min(sides) >= 4
    return gutter, two_col, crossing


@dataclass
class Page:
    pno: int
    rect: pymupdf.Rect
    lines: list
    gutter: float
    two_col: bool
    graphics: list = field(default_factory=list)
    regions: list = field(default_factory=list)   # dicts: kind, rect, anchor, ...


def load(doc):
    raws = [raw_lines(p) for p in doc]
    sizes = Counter()
    for page in raws:
        for spans in page:
            for s in spans:
                sizes[round(s.size, 1)] += len(s.text)
    body = sizes.most_common(1)[0][0] if sizes else 10.0
    pages = []
    cols = [find_gutter(rl, page.rect.width, body) if rl else (page.rect.width / 2, False, [])
            for page, rl in zip(doc, raws)]
    twos = sorted(c[0] for c in cols if c[1])
    mostly_two = len(twos) >= max(2, 0.25 * len(cols))
    for pno, (page, rl) in enumerate(zip(doc, raws)):
        gutter, two_col, crossing = cols[pno]
        if not two_col and mostly_two and crossing:        # figure/reference-heavy page of a 2-column paper:
            g = twos[len(twos) // 2]                        # keep the paper's gutter unless text runs across it
            if max(crossing[int(g) - 3:int(g) + 4]) <= 3:
                gutter, two_col = g, True
        pages.append(Page(pno, page.rect, visual_lines(rl, gutter, two_col), gutter, two_col))
    drop_running_lines(pages)
    set_columns(pages, body)
    global PITCH
    steps = [round((b.base - a.base) / body * 20) / 20 for p in pages for col in by_column(p.lines)
             for a, b in zip(col, col[1:]) if abs(a.size - body) < 0.6 and abs(b.size - body) < 0.6
             and len(a.text) > 40 and 0.9 * body < b.base - a.base < 3 * body]
    PITCH = Counter(steps).most_common(1)[0][0] if steps else 1.2
    return pages, body


def drop_running_lines(pages):
    """Running headers/footers: repeated (digits ignored) or page numbers, in the top 9% / bottom 10%."""
    def zone(p, ln):
        return ln.y1 < p.rect.height * 0.09 or ln.y0 > p.rect.height * 0.9
    key = lambda ln: re.sub(r"\d+", "#", ln.text.lower()).strip()
    seen = Counter(k for p in pages for k in {key(ln) for ln in p.lines if zone(p, ln)})
    limit = max(3, 0.25 * len(pages))
    for p in pages:
        if p.lines:
            lo, hi = min(ln.y0 for ln in p.lines), max(ln.y1 for ln in p.lines)
        p.lines = [ln for ln in p.lines
                   if not (zone(p, ln) and (seen[key(ln)] >= limit or PAGE_NUM_RE.match(ln.text)))
                   and not (PAGE_NUM_RE.match(ln.text) and (ln.y0 <= lo + 1 or ln.y1 >= hi - 1))]


def set_columns(pages, body):
    def bodyish(ln):
        return abs(ln.size - body) <= 1.2 and ln.gaps == 0 and len(ln.text) > 30
    for p in pages:
        for ln in p.lines:
            if p.two_col and ln.x1 <= p.gutter + 2:
                ln.col = "L"
            elif p.two_col and ln.x0 >= p.gutter - 2:
                ln.col = "R"
    bounds = {}
    for c in "LRF":
        sel = [ln for p in pages for ln in p.lines if ln.col == c and bodyish(ln)
               and (c != "F" or not p.two_col)]
        bounds[c] = (mode([ln.x0 for ln in sel], None), mode([ln.x1 for ln in sel], None))
    allx0 = mode([ln.x0 for p in pages for ln in p.lines if bodyish(ln)], 50)
    allx1 = mode([ln.x1 for p in pages for ln in p.lines if bodyish(ln)], 550)
    for p in pages:
        lb = (bounds["L"][0] or allx0, bounds["L"][1] or p.gutter - 6)
        rb = (bounds["R"][0] or p.gutter + 6, bounds["R"][1] or allx1)
        fb = (lb[0], rb[1]) if p.two_col else (bounds["F"][0] or allx0, bounds["F"][1] or allx1)
        for ln in p.lines:
            ln.cx0, ln.cx1 = {"L": lb, "R": rb}.get(ln.col, fb)


def reading_order(p: Page):
    def rows(lines):
        out, cur = [], []
        for ln in sorted(lines, key=lambda ln: ln.base):
            if cur and ln.base - cur[0].base > 0.4 * cur[0].size:
                out += sorted(cur, key=lambda l: l.x0)
                cur = []
            cur.append(ln)
        return out + sorted(cur, key=lambda l: l.x0)
    if not p.two_col:
        p.lines = rows(p.lines)
        return
    out, band = [], []
    for ln in sorted(p.lines, key=lambda ln: ln.y0):
        if ln.col == "F":
            out += rows([l for l in band if l.col == "L"]) + rows([l for l in band if l.col == "R"])
            band = []
            out.append(ln)
        else:
            band.append(ln)
    p.lines = out + rows([l for l in band if l.col == "L"]) + rows([l for l in band if l.col == "R"])


def by_column(lines):
    cols: dict[str, list] = {}
    for ln in lines:
        cols.setdefault(ln.col, []).append(ln)
    return [sorted(v, key=lambda l: l.base) for v in cols.values()]


def classify(p: Page, body: float, first_page: bool):
    lines = p.lines
    if first_page:
        cand = [ln for ln in lines if ln.y0 < p.rect.height / 2 and len(ln.text.split()) >= 2]
        top = max((ln.size for ln in cand), default=0)
        if top >= body + 1.5:
            first = min((ln for ln in cand if ln.size >= top - 0.5), key=lambda ln: ln.base)
            for ln in cand:                               # the title block: same size, consecutive lines
                if ln.size >= top - 0.5 and 0 <= ln.base - first.base <= 4 * top:
                    ln.kind = "title"
    mark_toc(lines)
    for ln in lines:
        if ln.kind:
            continue
        if CAPTION_RE.match(ln.text) and ln.size <= body + 0.6:
            ln.kind = "cap"
        elif is_heading(ln, body):
            ln.kind = "head"
        elif ln.size >= body - 1.6 and ln.gaps == 0 and ln.words >= 6 and ln.x0 <= ln.cx0 + 2 * ln.size:
            ln.kind = "text"
    # paragraph lines chained to text by normal line spacing; captions continue the same way
    for col in by_column(lines):
        def aligned(a, b):                               # same left edge, indent or hanging indent
            return min(a.x0, b.x0) <= a.cx0 + 1.6 * a.size and abs(a.x0 - b.x0) <= 2.5 * a.size
        for _ in range(2):
            for a, b in zip(col, col[1:]):                 # downwards
                if a.kind == "text" and not b.kind and chained(a, b) and aligned(a, b):
                    b.kind = "text"
            for a, b in reversed(list(zip(col, col[1:]))):  # upwards
                if b.kind == "text" and not a.kind and chained(a, b) and aligned(a, b):
                    a.kind = "text"
        # sub-figure captions: small-type paragraphs opening with "(a)" belong to the figure
        sub = False
        for a, b in zip([None] + col, col):
            if b.kind == "text" and b.size <= body - 0.5 and re.match(r"\([a-z]\)\s", b.text):
                sub = True
            elif not (sub and b.kind == "text" and a is not None and chained(a, b)):
                sub = False
            if sub:
                b.kind = "sub"
        for a, b in zip(col, col[1:]):
            if b.kind == "cap" and a.kind == "text" and chained(a, b) and not a.text.rstrip().endswith((".", ":")):
                b.kind = "text"                          # "... shown in / Fig. 2. The ..." is prose
    for a, b in zip(lines, lines[1:]):                   # a heading wrapped onto a second line
        if a.kind == b.kind == "head" and not SECNUM_RE.match(b.text) and a.bold == b.bold and chained(a, b, 0.6):
            a.text = f"{a.text} {b.text}"
            b.kind, b.claimed = "", True
    for a, b in zip(lines, lines[1:]):
        if a.kind == "cap" and b.kind in ("", "text", "sub") and chained(a, b, 0.5) and not CAPTION_RE.match(b.text):
            b.kind = "cap"
            b.level = -1                                 # caption continuation


def mark_toc(lines):
    """Contents rows end in a page number: after dot leaders, or alone at the right on the same baseline."""
    for ln in lines:
        m = re.match(r"^(.*?\S)(?:\s*\.){2,}\s*(\d+)$", ln.text)
        if m:
            ln.kind, ln.text = "toc", f"{m.group(1)} … {m.group(2)}"
    nums = [ln for ln in lines if re.fullmatch(r"\d{1,4}", ln.text) and ln.x1 >= ln.cx1 - 1.5 * ln.size]
    for n in nums:
        row = [ln for ln in lines if ln is not n and abs(ln.base - n.base) < 0.3 * n.size and ln.x1 < n.x0
               and ln.col == n.col and re.search(r"[A-Za-z]{3}", ln.text)]
        if len(row) == 1 and n.x0 - row[0].x1 > 2 * n.size:
            row[0].kind, row[0].text = "toc", f"{row[0].text} … {n.text}"
            n.kind, n.claimed = "toc", True
    for a, b in zip(lines, lines[1:]):                   # an entry wrapped onto two lines
        if b.kind == "toc" and not a.kind and SECNUM_RE.match(a.text) and 0 < b.base - a.base <= 2 * a.size:
            b.text = f"{a.text} {b.text}"
            a.kind, a.claimed = "toc", True


def keep_caption_style(pages):
    """Captions in one paper share a label word ("FIG.", "Figure", "Table"...). A candidate with another
    word is prose that happens to start a line ("Fig. 13. There is no ..."): turn it back into text."""
    word = lambda ln: CAPTION_RE.match(ln.text).group("label").split()[0].rstrip(".0123456789").rstrip(".")
    starts = [ln for p in pages for ln in p.lines if ln.kind == "cap" and ln.level != -1]
    usual = {}
    for kind in ("fig", "tab"):
        words = Counter(word(ln) for ln in starts if word(ln).lower().startswith(kind))
        if words and words.most_common(1)[0][1] >= 3:
            usual[kind] = words.most_common(1)[0][0]
    for p in pages:
        demote = False
        for ln in p.lines:
            if ln.kind == "cap" and ln.level != -1:
                w = word(ln)
                demote = usual.get(w.lower()[:3], w) != w
            if ln.kind == "cap" and demote:
                ln.kind, ln.level = "text", 0
            elif ln.kind != "cap":
                demote = False


def chained(a: Line, b: Line, slack=0.25) -> bool:
    """b is the next line of a's paragraph: normal line spacing, same size."""
    return (0 < b.base - a.base <= (PITCH + slack) * max(a.size, b.size)
            and abs(a.size - b.size) <= 1.0 and b.gaps == 0)


def is_heading(ln: Line, body: float) -> bool:
    t = ln.text.strip()
    if not re.search(r"[A-Za-z]{2}", t) or len(t) > 110 or len(t.split()) > 14 or ln.gaps > 1:
        return False
    if ln.size < body - 1.5:                             # plot labels, footnotes
        return False
    if t.endswith((",", ";")) or (t.endswith(".") and not SECNUM_RE.match(t + " x")):
        return False
    m = SECNUM_RE.match(t)
    plain = re.sub(r"^[\dIVX.\s]+", "", t).lower().rstrip(":")
    if ln.size >= body + 1.2 or (ln.bold and (m or len(t.split()) <= 8)):
        ln.level = heading_level(m, ln, body)
        return True
    if m and plain and (t.isupper() or ln.italic) and len(t) > 4 and ln.x0 <= ln.cx0 + 0.5 * ln.size:
        ln.level = heading_level(m, ln, body)
        return True
    if plain in UNNUMBERED_SECTIONS and ln.bold:
        ln.level = 2
        return True
    return False


def heading_level(m, ln, body):
    if m and m.group("d"):
        return min(5, 1 + m.group("d").count(".") + 1)
    if m and m.group("r"):
        return 2
    if m and m.group("a"):
        return 3
    return 2 if ln.size >= body + 2.5 else 3


# ----------------------------------------------------------------------------- figures, tables, equations

def page_graphics(page):
    """Boxes of everything painted that is not text (paths, images, shadings)."""
    area = page.rect.width * page.rect.height
    out = []
    for kind, bbox in page.get_bboxlog():
        if "text" in kind or kind.startswith(("clip", "ignore")):
            continue
        x0, y0, x1, y1 = bbox[0] - 0.5, bbox[1] - 0.5, bbox[2] + 0.5, bbox[3] + 0.5
        if x1 > 0 and y1 > 0 and x0 < page.rect.x1 and y0 < page.rect.y1 and (x1 - x0) * (y1 - y0) < 0.8 * area:
            out.append((x0, y0, x1, y1))
    return out


def x_span(p: Page, cap_lines):
    c = cap_lines[0]
    if p.two_col and c.col in "LR":
        return (c.cx0 - 6, min(c.cx1 + 6, p.gutter + 4)) if c.col == "L" else (max(c.cx0 - 6, p.gutter - 4), c.cx1 + 6)
    return (min(c.cx0, c.x0) - 20, max(c.cx1, c.x1) + 20)


def find_regions(p: Page, text_area):
    """Tables (caption + what follows) first, then figures (graphics above the caption)."""
    caps, cur = [], None
    for ln in p.lines:
        if ln.kind == "cap" and ln.level != -1:
            cur = [ln]
            caps.append(cur)
        elif ln.kind == "cap" and cur is not None and ln.col == cur[0].col:
            cur.append(ln)
    # paragraphs and captions bound a figure; "headings" are not trusted here (bold diagram labels look alike)
    def real_heading(i, ln):
        nxt = p.lines[i + 1] if i + 1 < len(p.lines) else None
        return ln.x0 <= ln.cx0 + 0.5 * ln.size or (
            nxt is not None and nxt.kind == "text" and 0 < nxt.base - ln.base <= 3 * PITCH * ln.size)
    walls = [ln.rect for i, ln in enumerate(p.lines) if ln.kind in ("text", "title", "cap")
             or (ln.kind == "head" and real_heading(i, ln))]
    ta0, ta1 = text_area
    for want in ("table", "fig"):
        for cl in caps:
            kind = CAPTION_RE.match(cl[0].text).group("kind").lower()
            if (kind == "table") != (want == "table"):
                continue
            crect = pymupdf.Rect(cl[0].rect)
            for ln in cl[1:]:
                crect |= ln.rect
            xr = x_span(p, cl)
            mine = [r for r in walls if not crect.contains(r)]
            over = lambda r: r.x1 > xr[0] + 2 and r.x0 < xr[1] - 2
            top = max([r.y1 for r in mine if over(r) and r.y1 <= crect.y0 + 1] + [ta0 - 4])
            bot = min([r.y0 for r in mine if over(r) and r.y0 >= crect.y1 - 1] + [ta1 + 4])
            above = pymupdf.Rect(xr[0], top, xr[1], crect.y0)
            below = pymupdf.Rect(xr[0], crect.y1, xr[1], bot)
            order = (below, above) if want == "table" else (above, below)
            for win in order:
                ink, labels = contents(p, win)
                if ink or (labels and want == "table"):
                    rect = pymupdf.Rect(crect) if want == "table" else pymupdf.Rect()
                    for r in ink + [ln.rect for ln in labels]:
                        rect |= pymupdf.Rect(r) & win
                    lim = pymupdf.Rect(win.x0, win.y0, win.x1, win.y1) | crect if want == "table" else win
                    rect = (rect + (-2, -2, 2, 2)) & lim
                    break
            else:
                rect = None
            region = {"kind": want, "caption": cl, "rect": rect, "anchor": cl[0]}
            p.regions.append(region)
            for ln in cl:
                ln.claimed = True
            if rect is not None:
                for ln in p.lines:
                    if ln.kind != "cap" and not ln.claimed and rect.contains(
                            pymupdf.Point((ln.x0 + ln.x1) / 2, (ln.y0 + ln.y1) / 2)):
                        ln.claimed = True
                        region.setdefault("lines", []).append(ln)
                walls.append(rect)


def contents(p: Page, win):
    """Painted boxes (clipped to win) and unclassified text lines inside a window."""
    if win.height < 2:
        return [], []
    wx0, wy0, wx1, wy1 = win
    ink = []
    for g in p.graphics:
        ix0, iy0, ix1, iy1 = max(g[0], wx0), max(g[1], wy0), min(g[2], wx1), min(g[3], wy1)
        if ix1 - ix0 > 1 and iy1 - iy0 > 0.5:
            ink.append((ix0, iy0, ix1, iy1))
    labels = [ln for ln in p.lines if ln.kind in ("", "head", "sub") and not ln.claimed and win.contains(ln.rect)]
    return ink, labels


def find_equations(p: Page, body: float):
    """A numbered equation = the run of non-prose lines around its "(n)" between paragraphs.
    Unnumbered display math stays as text."""
    for col in by_column(p.lines):
        runs, cur = [], []
        for ln in col:
            free = not ln.kind and not ln.claimed and (ln.words <= 2 or ln.tag)
            if free and (not cur or ln.base - cur[-1].base <= (PITCH + 1.8) * body):
                cur.append(ln)
                continue
            if cur:
                runs.append(cur)
            cur = [ln] if free else []
        if cur:
            runs.append(cur)
        for run in runs:
            tags = [ln.tag for ln in run if ln.tag]
            if not tags:
                continue
            rect = pymupdf.Rect(min(min(ln.x0, ln.cx0) for ln in run), min(ln.y0 for ln in run),
                                max(max(ln.x1, ln.cx1) for ln in run), max(ln.y1 for ln in run))
            for g in p.graphics:                            # fraction bars, radical rules
                if g[3] - g[1] < 3 and g[2] > rect.x0 and g[0] < rect.x1 and g[3] > rect.y0 - 3 and g[1] < rect.y1 + 3:
                    rect |= g
            for ln in run:
                ln.claimed = True
            p.regions.append({"kind": "eq", "rect": rect, "anchor": run[0], "lines": run, "tags": tags})


# ----------------------------------------------------------------------------- author lists

ORG_WORDS = re.compile(r"\b(?:Foundation|Council|Ministry|Agency|Sciences?|Research|National|Grants?|Programm?e?|"
                       r"Fund|Office|Department|Society|Institute|University|Laboratory|Cent(?:er|re)|Academy|"
                       r"Collaboration|Committee|Commission|Republic|Government|Federal|Education|Technology|Energy)\b")


def name_like(chunk: str) -> bool:
    """A person's name: 2-5 capitalised tokens or initials ("M. Abumusabh", "Jenni Ott"), no agency words."""
    w = chunk.split()
    if not 2 <= len(w) <= 5 or ORG_WORDS.search(chunk) or any(c.isdigit() for c in chunk):
        return False
    ok = all((x[0].isupper() and re.fullmatch(r"[\w'’.\-]+", x)) or x.lower() in PARTICLES for x in w)
    return ok and any(len(x) > 2 and not x.endswith(".") for x in w)


def author_kind(ln: Line) -> str:
    t = re.sub(r"[∗†‡§¶*]", "", SCRIPT_RE.sub("", ln.text))
    chunks = [c.strip() for c in re.split(r",|\band\b", t) if c.strip()]
    good = sum(name_like(c) for c in chunks)
    if len(chunks) >= 2 and good >= 0.75 * len(chunks):
        return "names"
    if len(chunks) == 1 and good and len(t.split()) <= 4:
        return "name"
    hits = len(AFFIL_RE.findall(t))
    if hits >= 2 or (hits and re.match(r"^\s*(?:\d{1,3}|[a-z])\s+[A-Z]", ln.text)) or (
            hits and re.match(r"^\s*\^\{", ln.text)):
        return "affil"
    return ""


def split_names(text):
    return [c.strip() for c in re.split(r",|\band\b", re.sub(r"[∗†‡§¶*]", "", SCRIPT_RE.sub("", text)))
            if name_like(c.strip())]


def collapse_author_lists(flow):
    """A run that starts with a multi-name line and holds >= 3 of them (affiliations may sit
    between or after) becomes one placeholder line."""
    out, run, misses = [], [], []

    def close():
        lines = [x for x in run if isinstance(x, Line)]
        multi = [x for x in lines if author_kind(x) == "names"]
        names = [n for x in lines if author_kind(x) in ("names", "name") for n in split_names(x.text)]
        if len(multi) >= 3 and len(names) >= 10:
            out.append({"kind": "authors", "n": len(names), "first": split_names(multi[0].text)[:3]})
            out.extend(x for x in run if not isinstance(x, Line))      # page markers survive
        else:
            out.extend(run)
        out.extend(misses)
        run.clear()
        misses.clear()

    for item in flow:
        line = isinstance(item, Line)
        kind = author_kind(item) if line and item.kind in ("", "text") else ""
        if kind and (run or kind == "names"):
            run.extend(misses)
            misses.clear()
            run.append(item)
        elif run and (not line or (len(misses) < 2 and item.kind not in ("head", "title"))):
            misses.append(item)
        else:
            if run:
                close()
            out.append(item)
    if run:
        close()
    return out


# ----------------------------------------------------------------------------- glyph engine
# textblocks/inline rebuild each line glyph by glyph (stacked scripts, raw TeX sums/brackets, vector
# accents, overlines, inline fractions) and tablekit builds Markdown tables. Both speak $..$ LaTeX,
# which is written back here in the plain notation used everywhere else: p⃗_{miss}, 0.5 cm, 10^{−5}.
from . import inline as pt_inline, tablekit as pt_tablekit
from .texmap import UNI2TEX
from .textblocks import _line_main as line_main
from .textblocks import decode_raw_cmex_document, mark_overlines, merge_fragments, page_infos

TEX2UNI = {v: k for k, v in UNI2TEX.items() if v.startswith("\\") and "{" not in v}
TEX2UNI.update({"\\mathcal{L}": "ℒ", "\\to": "→", "\\leq": "≤", "\\geq": "≥", "\\le": "≤", "\\ge": "≥",
                "\\ldots": "…", "\\cdots": "⋯", "\\dots": "…", "\\prime": "′", "\\{": "{", "\\}": "}"})
TEX_ACCENTS = {"vec": "⃗", "bar": "̄", "overline": "̄", "hat": "̂", "widehat": "̂",
               "tilde": "̃", "widetilde": "̃", "dot": "̇", "ddot": "̈", "check": "̌",
               "breve": "̆", "acute": "́", "grave": "̀"}
TEX_WRAPPERS = {"text", "textrm", "textit", "textbf", "mathrm", "mathit", "mathbf", "mathsf", "mathtt",
                "mathcal", "mathbb", "boldsymbol", "bm", "operatorname", "mbox"}
TEX_SILENT = {"left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr",
              "displaystyle", "textstyle", "scriptstyle", "limits", "nolimits"}
SPACED = set("=<>≤≥≈∼≃≡→←↔⇒∈±×−+")


def _tex_arg(s: str, i: int) -> tuple[str, int]:
    """The {group}, \\command or single character at s[i:] -> (content, index after)."""
    while i < len(s) and s[i] == " ":
        i += 1
    if i >= len(s):
        return "", i
    if s[i] == "{":
        depth = 0
        for j in range(i, len(s)):
            depth += {"{": 1, "}": -1}.get(s[j], 0)
            if depth == 0:
                return s[i + 1:j], j + 1
        return s[i + 1:], len(s)
    m = re.match(r"\\[A-Za-z]+|\\.|.", s[i:])
    return m.group(0), i + m.end()


def _wrap(x: str) -> str:
    """Parenthesise a fraction part or radicand unless it is one term."""
    flat = x
    while re.search(r"\([^()]*\)", flat):
        flat = re.sub(r"\([^()]*\)", "", flat)
    flat = SCRIPT_RE.sub("", flat)
    return f"({x})" if re.search(r"[\s+\-−×/=<>±]", flat) else x


def tex_to_plain(s: str) -> str:
    """The glyph engine's inline LaTeX -> Unicode with _{..}/^{..}."""
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            m = re.match(r"\\([A-Za-z]+) ?|\\(.)", s[i:])
            i += m.end()
            name = m.group(1)
            if name is None:
                out.append({",": " ", ";": " ", ":": " ", "!": "", " ": " ", "|": "‖"}.get(m.group(2), m.group(2)))
            elif name in TEX_SILENT:
                continue
            elif name in TEX_WRAPPERS:
                arg, i = _tex_arg(s, i)
                out.append(tex_to_plain(arg))
            elif name in TEX_ACCENTS:
                arg, i = _tex_arg(s, i)
                a = tex_to_plain(arg)
                mark = TEX_ACCENTS[name]
                out.append("".join(ch + mark for ch in a) if name == "overline" else a + mark)
            elif name == "frac":
                a, i = _tex_arg(s, i)
                b, i = _tex_arg(s, i)
                out.append(f"{_wrap(tex_to_plain(a))}/{_wrap(tex_to_plain(b))}")
            elif name == "sqrt":
                a, i = _tex_arg(s, i)
                out.append(f"√{_wrap(tex_to_plain(a))}")
            elif name in ("quad", "qquad"):
                out.append(" ")
            else:
                sym = TEX2UNI.get("\\" + name, name)
                out.append(sym + " " if sym in "∑∏∫∮⋃⋂" else sym)
        elif c == "{":
            arg, i = _tex_arg(s, i)
            out.append(tex_to_plain(arg))
        elif c in "^_":
            arg, i = _tex_arg(s, i + 1)
            t = tex_to_plain(arg).strip()
            t = {"'": "′", "''": "″"}.get(t, t)
            out.append("°" if c == "^" and t in ("∘", "◦") else t if t in ("′", "″") else f"{c}{{{t}}}")
        else:
            out.append({"-": "−", "~": " "}.get(c, c))
            i += 1
    return "".join(out)


def _space_relations(t: str) -> str:
    """One space around = < → ± × − + at brace depth 0 (not in scripts, not unary)."""
    out, depth = [], 0
    for k, c in enumerate(t):
        depth += {"{": 1, "}": -1}.get(c, 0)
        if depth == 0 and c in SPACED:
            prev = "".join(out).rstrip()
            unary = c in "−+±" and (not prev or prev[-1] in "([{=<>≤≥→,|")
            if not prev and not unary:                   # "× 10^{-5}" opening its own math run
                out.append(c + ("" if k + 1 < len(t) and t[k + 1] == " " else " "))
                continue
            if not unary and prev:
                out = list(prev) + [" ", c]
                if k + 1 < len(t) and t[k + 1] != " ":
                    out.append(" ")
                continue
        out.append(c)
    return "".join(out)


def detex(md: str) -> str:
    """Every $..$ run of the glyph engine's Markdown in plain notation; sentinels and stray marks removed."""
    md = re.sub(r"(?<!\\)\$(.+?)(?<!\\)\$", lambda m: _space_relations(tex_to_plain(m.group(1))), md)
    md = "".join(ch for ch in md if unicodedata.category(ch) != "Co")
    md = re.sub(r"(?<=[a-z]{2})\u0305|\u0305(?=[a-z]{2})", "", md)   # an "overline" inside a word is a stray rule
    md = re.sub(r"<sup>(.*?)</sup>", r"^{\1}", md)
    md = re.sub(r"<sub>(.*?)</sub>", r"_{\1}", md)
    return fix_accents(re.sub(r"[ \t]+", " ", md).strip())


class GlyphEngine:
    """The glyph engine's text for the layout's lines, page by page."""

    def __init__(self, doc):
        self.infos = [page_infos(p) for p in doc]
        decode_raw_cmex_document(self.infos)
        pt_inline.TEXT_ITALIC_MATH = pt_inline.detect_text_italic_math(self.infos)

    def retext(self, p: Page):
        """Swap in the glyph engine's rendering for lines it covers with one visual line or more."""
        bars = [pymupdf.Rect(g) for g in p.graphics if g[3] - g[1] <= 2.2 and g[2] - g[0] >= 3]
        boxes = [(L.x0, L.y0, L.x1, L.y1) for L in p.lines]
        got: dict[int, list] = {}
        for info in merge_fragments(self.infos[p.pno]):
            near = info.rect + (-1, -4, 1, 1)
            mark_overlines(info.lines, [b for b in bars if b.intersects(near)])
            for ln in info.lines:
                x0, y0, x1, y1 = ln["bbox"]
                if abs(ln["dir"][0] - 1) > 1e-3 or x1 <= x0:
                    continue
                base, size = line_main(ln)
                best, dist = None, 1e9
                for k, (a0, b0, a1, b1) in enumerate(boxes):      # the layout line with the nearest baseline
                    w = min(x1, a1) - max(x0, a0)
                    d = abs(p.lines[k].base - base)
                    if b0 - 1 <= base <= b1 + 1 and w >= 0.8 * (x1 - x0) and d < dist:
                        best, dist = k, d
                if best is not None:
                    got.setdefault(best, []).append((pymupdf.Rect(x0, y0, x1, y1), base, size, ln))
        for k, parts in got.items():
            L = p.lines[k]
            parts.sort(key=lambda x: x[0].x0)
            if sum(x[0].width for x in parts) < 0.85 * (L.x1 - L.x0):
                continue                                     # the engine saw only part of this line
            main = max(parts, key=lambda x: (round(x[2], 1), x[0].width))
            text, prev_x1 = "", None
            for r, base, size, ln in parts:
                t = detex(pt_inline.render_tokens(pt_inline.line_tokens(ln)))
                script = (text and size <= 0.85 * main[2] and abs(base - main[1]) > 0.1 * main[2]
                          and r.x0 - prev_x1 < 0.5 * main[2] and " " not in t and t[:1] not in "∑∏∫")
                prev_x1 = r.x1
                if script:
                    text += ("_{" if base > main[1] else "^{") + t + "}"   # a script MuPDF cut off
                else:
                    text = f"{text} {t}".strip()
            if text:
                L.text = text
                L.words = len(LONG_WORD_RE.findall(SCRIPT_RE.sub(" ", text)))

    def tables(self, page, p: Page, body: float):
        """tablekit's tables on this page: [(rect, label, markdown)]."""
        bars = [pymupdf.Rect(g) for g in p.graphics if g[3] - g[1] <= 2.2 and g[2] - g[0] >= 3]
        vert = [({"items": [("re", pymupdf.Rect(g))]}, pymupdf.Rect(g)) for g in p.graphics
                if g[2] - g[0] <= 1.6 and g[3] - g[1] >= 6]
        try:
            found = pt_tablekit.find_tables(page, p.pno, self.infos[p.pno], bars, vert, body)
        except Exception:                                    # a table it cannot read keeps the raw rows
            return []
        return [(t.rect, t.label, detex(t.to_markdown())) for t in found
                if not {"no-region", "single-column", "no-body"} & set(t.flags)]


# ----------------------------------------------------------------------------- markdown

def caption_md(lines) -> str:
    text = join_lines(lines)
    m = CAPTION_RE.match(text)
    return f"**{m.group('label').strip()}**{text[m.end():]}" if m else text


def join_lines(lines) -> str:
    out = ""
    for ln in lines:
        t = DOTS_RE.sub(" … ", ln.text).strip()
        if re.search(r"[A-Za-z]-$", out):                 # hyphenated line end
            out = out[:-1] + t if t[:1].islower() else out + t
        else:
            out = f"{out} {t}" if out else t
    return out


def new_paragraph(para: list, b: Line) -> bool:
    a, first = para[-1], para[0]
    right = max([ln.x1 for ln in para if ln.col == a.col] + [b.x1])   # the paragraph's own right margin
    if LIST_RE.match(b.text) or b.gaps or a.gaps or DOTS_RE.search(a.text):
        return True
    if LIST_RE.match(first.text) and b.x0 > first.x0 + 0.3 * b.size and b.x0 < first.x0 + 4 * b.size:
        return False                                     # hanging indent (reference lists)
    if abs(a.size - b.size) > 0.8:
        return True
    if a.col != b.col or b.base < a.base:                # next column: continue unless indented
        return b.x0 > b.cx0 + 0.5 * b.size or a.x1 < a.cx1 - 2 * a.size
    gap = b.base - a.base
    open_end = not re.search(r"[.:;!?]\W*$", a.text)            # sentence still running ("... = p⃗ −")
    return ((gap > (PITCH + 0.4) * a.size and not (open_end and gap <= (PITCH + 1.2) * a.size))
            or a.x1 < right - 2.5 * a.size or (b.x0 > a.x0 + 0.6 * b.size and not open_end))


def table_rows(region) -> str:
    lines = sorted(region.get("lines", []), key=lambda l: l.base)
    rows, cur = [], []
    for ln in lines:
        if cur and ln.base - cur[-1].base > 0.5 * max(ln.size, cur[-1].size):
            rows.append(cur)
            cur = []
        cur.append(ln)
    if cur:
        rows.append(cur)
    out = []
    for row in rows:
        spans = sorted((s for ln in row for s in ln.spans), key=lambda s: s.x0)
        m = max(row, key=lambda l: len(l.text))
        out.append(render(spans, m.size, m.base, cell_gap=0.9 * m.size))
    return "\n".join(out)


def save_crop(page, rect, path: Path, dpi, images: bool, cache: dict, pad=2):
    rect = (rect + (-pad, -pad, pad, pad)) & page.rect
    if images and not rect.is_empty:
        path.parent.mkdir(parents=True, exist_ok=True)
        if page.number not in cache:                     # one display list per page, many crops
            cache.clear()
            cache[page.number] = page.get_displaylist()
        z = dpi / 72
        cache[page.number].get_pixmap(matrix=pymupdf.Matrix(z, z), clip=rect).save(path)
    return [round(v, 1) for v in rect]


def convert(pdf: str, out_root: str | Path = PAPER_DIR, dpi: int = 200, images: bool = True,
            keep_authors: bool = False) -> dict:
    """Convert one PDF; returns a summary dict (pages, figures, tables, equations, chars, seconds)."""
    t0 = time.time()
    pdf = Path(pdf)
    out = Path(out_root) / pdf.stem
    out.mkdir(parents=True, exist_ok=True)
    for sub in ("images", "tables", "equations"):        # no stale crops from earlier runs
        shutil.rmtree(out / sub, ignore_errors=True)
    doc = pymupdf.open(pdf)
    pages, body = load(doc)
    body_lines = [ln for p in pages for ln in p.lines if abs(ln.size - body) <= 1.5]
    text_area = (min((ln.y0 for ln in body_lines), default=0),
                 max((ln.y1 for ln in body_lines), default=doc[0].rect.height))
    engine = GlyphEngine(doc)
    for p in pages:
        p.graphics = page_graphics(doc[p.pno])
        engine.retext(p)
        reading_order(p)
        classify(p, body, p.pno == 0)
    keep_caption_style(pages)
    flow = []
    for p in pages:
        p.pt_tables = engine.tables(doc[p.pno], p, body)
        find_regions(p, text_area)
        find_equations(p, body)
        anchors = {id(r["anchor"]): r for r in p.regions}
        flow.append({"kind": "page", "page": p.pno + 1})
        for ln in p.lines:
            ln.page = p.pno + 1
            if id(ln) in anchors:
                flow.append(anchors[id(ln)] | {"page": p.pno + 1})
            elif not ln.claimed and ln.text.strip():
                flow.append(ln)
    if not keep_authors:
        flow = collapse_author_lists(flow)

    index = {"source": pdf.name, "pages": len(pages), "title": "", "headings": [], "figures": [],
             "tables": [], "equations": [], "authors_omitted": 0}
    md, para, eq_seen, dl_cache = [], [], Counter(), {}

    pending = []                                         # page markers that fall inside a paragraph

    def flush():
        if para:
            md.append(join_lines(para))
            para.clear()
        md.extend(pending)
        pending.clear()

    title = [x for x in flow if isinstance(x, Line) and x.kind == "title"]
    if title:
        index["title"] = join_lines(title)
        md.append("# " + index["title"])
    for item in flow:
        if isinstance(item, Line):
            if item.kind == "title":
                continue
            if item.kind == "toc":
                flush()
                md.append(f"- {item.text}")
                continue
            if item.kind == "head":
                flush()
                md.append("#" * item.level + " " + item.text)
                index["headings"].append({"level": item.level, "text": item.text, "page": item.page})
                continue
            if para and new_paragraph(para, item):
                flush()
            if item.gaps:
                flush()
                md.append(render(item.spans, item.size, item.base, cell_gap=1.5 * item.size))
                continue
            para.append(item)
            continue
        k, pg = item["kind"], item.get("page")
        if k == "page" and para:
            pending.append(f"<!-- page {pg} -->")
            continue
        flush()
        page = doc[pg - 1] if pg else None
        if k == "page":
            md.append(f"<!-- page {pg} -->")
        elif k == "authors":
            index["authors_omitted"] += item["n"]
            md.append(f"*Author list omitted ({item['n']} names: {', '.join(item['first'])}, …)*")
        elif k in ("fig", "table"):
            m = CAPTION_RE.match(item["caption"][0].text)
            num, label = m.group("num"), ("Figure " if k == "fig" else "Table ") + m.group("num")
            cap = caption_md(item["caption"])
            rec = {"label": label, "page": pg, "caption": join_lines(item["caption"]), "file": None, "bbox": None}
            if item["rect"] is not None:
                sub = "images" if k == "fig" else "tables"
                name = f"{sub}/{'fig' if k == 'fig' else 'table'}_{num}_p{pg}.png"
                while (out / name).exists():                 # never overwrite another crop
                    name = name[:-4] + "b.png"
                rec["bbox"] = save_crop(page, item["rect"], out / name, dpi, images, dl_cache, pad=0)
                rec["file"] = name
            link = f"![{label}]({rec['file']})" if rec["file"] else f"*[{label}: no crop found on page {pg}]*"
            if k == "fig":
                subs = [ln for ln in item.get("lines", []) if ln.kind == "sub"]
                if subs:
                    cap += " " + join_lines(subs)
                    rec["caption"] += " " + join_lines(subs)
                md += [link, cap]
                index["figures"].append(rec)
            else:
                built = [t for t in getattr(pages[pg - 1], "pt_tables", [])
                         if item["rect"] is not None and pymupdf.Rect(t[0]).intersects(item["rect"])]
                if built:                                    # tablekit: a real Markdown table
                    rows = built[0][2]
                    md += [cap, link, rows]
                else:                                        # raw rows, cells split at wide gaps
                    rows = table_rows(item)
                    md += [cap, link] + ([f"```text\n{rows}\n```"] if rows else [])
                rec["text"] = rows
                index["tables"].append(rec)
        elif k == "eq":
            tags = item["tags"]
            stem = "-".join(tags) if tags else f"p{pg}_{len(index['equations']) + 1}"
            eq_seen[stem] += 1
            stem += f"_{eq_seen[stem]}" if eq_seen[stem] > 1 else ""
            name = f"equations/eq_{stem}_p{pg}.png" if tags else f"equations/eq_{stem}.png"
            bbox = save_crop(page, item["rect"], out / name, dpi, images, dl_cache)
            alt = f"Equation ({', '.join(tags)})" if tags else "Equation"
            md.append(f"![{alt}]({name})")
            index["equations"].append({"numbers": tags, "page": pg, "file": name, "bbox": bbox,
                                       "text": " / ".join(l.text for l in item["lines"])})
    flush()
    text = "\n\n".join(x for x in md if x.strip()) + "\n"
    (out / f"{pdf.stem}.md").write_text(text, encoding="utf-8")
    (out / "index.json").write_text(json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"pdf": pdf.name, "pages": len(pages), "figures": len(index["figures"]),
            "tables": len(index["tables"]), "equations": len(index["equations"]),
            "chars": len(text), "seconds": round(time.time() - t0, 1)}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="paperlite", description="PDF -> Markdown for LLM reading")
    ap.add_argument("pdfs", nargs="*",
                    help=f"PDFs to convert (default: every *.pdf in {PAPER_DIR})")
    ap.add_argument("-o", "--out", default=str(PAPER_DIR),
                    help="output root; one folder per paper (default: %(default)s)")
    ap.add_argument("-j", "--jobs", type=int, default=1)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--no-images", action="store_true", help="text + index only (fastest)")
    ap.add_argument("--keep-authors", action="store_true",
                    help="keep full author/affiliation lists instead of one summary line")
    a = ap.parse_args(argv)
    pymupdf.TOOLS.mupdf_display_errors(False)
    pdfs = a.pdfs or sorted(str(p) for p in PAPER_DIR.glob("*.pdf"))
    if not pdfs:
        ap.error(f"no PDFs given and none found in {PAPER_DIR}")
    args = [(p, a.out, a.dpi, not a.no_images, a.keep_authors) for p in pdfs]
    if a.jobs > 1 and len(args) > 1:
        with ProcessPoolExecutor(a.jobs) as ex:
            results = list(ex.map(convert, *zip(*args)))
    else:
        results = [convert(*x) for x in args]
    for r in results:
        print(json.dumps(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
