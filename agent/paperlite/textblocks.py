"""
Text blocks: one pass over MuPDF's rawdict, plus the repairs MuPDF's own block
segmentation needs on TeX output.

* merge_fragments   - a stacked script (M^true_Xs) makes MuPDF cut a paragraph into
                      overlapping blocks and one visual line into several lines;
                      glue them back together so the paragraph renders as one.
* attach_accents    - TeX draws \\bar{\\nu} as two glyphs ("ν", "¯"); fold the accent
                      into the letter as a combining mark (-> \\bar{\\nu} later).
* mark_overlines    - \\overline{B} is a drawn rule, not a glyph; mark the letters
                      under a short rule the same way.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .core import (
    Rect, TEXT_FLAGS, caption_label, clean_text, pymupdf, span_bold, span_mono,
    union_rects,
)
from .texmap import (
    ACCENT_GLYPHS, GLUE_CHARS, MATH_CHARS, RAD_CLOSE, RAD_OPEN, RAW_OPEN, RAW_CLOSE, decode_cmex,
)
from .core import EXT_FONT_RE, MATH_FONT_RE, base_font


def span_is_math(font: str, text: str) -> bool:
    """A span is math if set in a math font, or if it is (mostly) math symbols."""
    core = text.replace(" ", "")
    if not core or core in {"•", "◦", "∗"}:
        return False
    if MATH_FONT_RE.search(base_font(font)):
        return True
    n_math = sum(c in MATH_CHARS for c in core)
    return n_math > 0 and n_math / len(core) >= 0.5


@dataclass
class BlockInfo:
    lines: list            # PyMuPDF line dicts (horizontal lines only, unless all rotated)
    rect: Rect
    text: str              # plain text, lines joined by spaces
    size: float            # char-weighted dominant font size
    n_lines: int
    bold_frac: float
    mono_frac: float
    math_frac: float       # chars in math fonts / math symbols
    mathish_frac: float    # math chars + digits/operators/brackets
    long_words: int        # words of >= 4 letters set in text fonts (equations have few)
    rotated: bool          # every line is rotated (arXiv stamp, y-axis label)
    display: bool = False  # assembled display equation (equations.assemble_display_equations)

    @property
    def first_line(self) -> str:
        return "".join(s["text"] for s in self.lines[0]["spans"]).strip() if self.lines else ""

    @property
    def caption(self) -> str | None:
        return caption_label(self.text, self.first_line)


def is_horizontal(line: dict) -> bool:
    dx, dy = line.get("dir", (1, 0))
    return abs(dy) < 0.05 and dx > 0


def analyze_block(lines_in: list) -> BlockInfo | None:
    """Summarise a block: text, dominant size, how bold/mono/mathy it is."""
    lines = [ln for ln in lines_in if is_horizontal(ln)]
    rotated = not lines
    if rotated:
        lines = lines_in
    sizes, n = Counter(), 0
    bold = mono = math_s = math_l = long_words = 0
    texts = []
    for ln in lines:
        texts.append("".join(s["text"] for s in ln["spans"]).strip())
        for s in ln["spans"]:
            t = clean_text(s["text"])
            core = t.replace(" ", "")
            if not core:
                continue
            k = len(core)
            n += k
            sizes[round(s["size"], 1)] += k
            bold += k * span_bold(s)
            mono += k * span_mono(s)
            if span_is_math(s["font"], t):
                math_s += k
                math_l += k
            else:
                math_l += sum(c in GLUE_CHARS for c in core)
                long_words += len(re.findall(r"[A-Za-z]{4,}", t))
    if n == 0:
        return None
    return BlockInfo(
        lines=lines,
        rect=union_rects(Rect(ln["bbox"]) for ln in lines),
        text=clean_text(" ".join(t for t in texts if t)),
        size=sizes.most_common(1)[0][0],
        n_lines=len(lines),
        bold_frac=bold / n,
        mono_frac=mono / n,
        math_frac=math_s / n,
        mathish_frac=math_l / n,
        long_words=long_words,
        rotated=rotated,
    )


def simplify_span(span: dict) -> dict:
    """
    rawdict span -> "dict"-style span with honest geometry.

    MuPDF synthesises space characters that inherit the *previous* glyph's
    position, so a span like " and" right after a subscript reports a lowered
    baseline. Taking origin/bbox from the inked characters fixes that; the
    per-char boxes are kept for accents and the 2-D equation builder.
    """
    chars = span.get("chars", [])
    cmex = None
    if EXT_FONT_RE.search(base_font(span["font"])):
        cmex = "raw" if decode_cmex(chars) else "unknown"
        chars = _stitch_pieces([c for c in chars if c["c"]])
    out = {
        "text": "".join(c["c"] for c in chars),
        "font": span["font"], "size": span["size"], "flags": span["flags"],
        "origin": span["origin"], "bbox": span["bbox"], "chars": chars, "cmex": cmex,
    }
    _refresh_geometry(out)
    return out


def _stitch_pieces(chars: list[dict]) -> list[dict]:
    """A tall CMEX delimiter is drawn as stacked copies of one piece at one x
    ("|" over "|"): keep a single glyph, or "|p|" reads as "||p||"."""
    out: list[dict] = []
    for c in chars:
        p = out[-1] if out else None
        if (p is not None and p["c"] == c["c"] and not c["c"].isspace()
                and abs(p["bbox"][0] - c["bbox"][0]) < 0.6 and c["bbox"][1] > p["bbox"][1]):
            p["bbox"] = tuple(Rect(p["bbox"]) | Rect(c["bbox"]))
            continue
        out.append(c)
    return out


def _refresh_geometry(s: dict) -> None:
    ink = [c for c in s["chars"] if not c["c"].isspace() and not Rect(c["bbox"]).is_empty]
    if ink:
        s["origin"] = ink[0]["origin"]
        s["bbox"] = tuple(union_rects(Rect(c["bbox"]) for c in ink))


def page_infos(page: pymupdf.Page) -> list[BlockInfo]:
    """All text blocks of a page, analysed (rawdict = per-character positions)."""
    infos = []
    for b in page.get_text("rawdict", flags=TEXT_FLAGS)["blocks"]:
        if b.get("type", 0) != 0:
            continue
        lines = [
            {"bbox": ln["bbox"], "dir": ln["dir"], "spans": [simplify_span(s) for s in ln["spans"]]}
            for ln in b.get("lines", [])
        ]
        for ln in lines:
            attach_accents(ln)
        info = analyze_block(lines)
        if info:
            infos.append(info)
    return infos


def decode_raw_cmex_document(all_infos: list[list["BlockInfo"]]) -> None:
    """
    A CMEX font is either Unicode-mapped or raw for the whole document. Spans with
    only "!" (Bigg ")") or a real space glyph (Bigg "(") can't reveal that alone,
    so once any span of a font looks raw, decode every span of that font.
    """
    raw_fonts = {s["font"] for infos in all_infos for i in infos for ln in i.lines
                 for s in ln["spans"] if s.get("cmex") == "raw"}
    if not raw_fonts:
        return
    for infos in all_infos:
        for k, i in enumerate(infos):
            touched = False
            for ln in i.lines:
                for s in ln["spans"]:
                    if s.get("cmex") == "unknown" and s["font"] in raw_fonts:
                        decode_cmex(s["chars"], force=True)
                        s["chars"] = _stitch_pieces([c for c in s["chars"] if c["c"]])
                        s["text"] = "".join(c["c"] for c in s["chars"])
                        s["cmex"] = "raw"
                        _refresh_geometry(s)
                        touched = True
            if touched:
                infos[k] = analyze_block(i.lines) or i


# --------------------------------------------------------------------------- #
# Accents                                                                     #
# --------------------------------------------------------------------------- #
def attach_accents(line: dict) -> None:
    """
    'ν' '¯' 'ν'  ->  'ν' 'ν̄'. The accent glyph sits *over* its base letter, so
    the two overlap horizontally (ordinary neighbours never do by more than a
    kern). Glyph boxes are not vertically tight, so x-overlap is the signal.
    """
    flat = [(s, c) for s in line["spans"] for c in s["chars"]]
    accents = [c for s, c in flat if c["c"] in ACCENT_GLYPHS and not span_mono(s)]
    if not accents:
        return
    removed: set[int] = set()
    size_of = {id(c): s["size"] for s, c in flat}
    for a in accents:
        ax0, _, ax1, _ = a["bbox"]
        aw = ax1 - ax0
        if aw <= 0.2:
            # zero-width combining glyph (\vec arrow): it sits up to ~0.5 em left
            # of its base letter, on the same baseline (found by geometry, since
            # after fragment merging the stream order is no longer reliable)
            sz = size_of[id(a)]
            cands = [c for _, c in flat if c is not a and id(c) not in removed
                     and not c["c"].isspace() and c["c"][:1] not in ACCENT_GLYPHS
                     and -1.0 <= c["bbox"][0] - ax1 <= 0.8 * sz
                     and abs(c["origin"][1] - a["origin"][1]) < 0.3 * sz]
            if cands:
                nxt = min(cands, key=lambda c: c["bbox"][0])
                nxt["c"] += ACCENT_GLYPHS[a["c"]]
                removed.add(id(a))
            continue
        best, best_ov = None, 0.0
        for _, c in flat:
            if c is a or id(c) in removed or c["c"].isspace() or c["c"] in ACCENT_GLYPHS:
                continue
            cx0, _, cx1, _ = c["bbox"]
            ov = min(ax1, cx1) - max(ax0, cx0)
            if ov > 0 and ov / max(0.1, min(aw, cx1 - cx0)) >= 0.5 and ov > best_ov:
                best, best_ov = c, ov
        if best is not None:
            best["c"] += ACCENT_GLYPHS[a["c"]]
            removed.add(id(a))
    if removed:
        for s in line["spans"]:
            s["chars"] = [c for c in s["chars"] if id(c) not in removed]
            s["text"] = "".join(c["c"] for c in s["chars"])
            _refresh_geometry(s)
        line["spans"] = [s for s in line["spans"] if s["chars"]]


def mark_overlines(lines: list, bars: list[Rect]) -> None:
    """
    A short rule just above full-size letters, with nothing small right above
    it (that would be a fraction numerator), is an overline: B B̄ -> B B̅.
    """
    if not bars:
        return
    chars = [(s, c) for ln in lines for s in ln["spans"] for c in s["chars"]
             if not c["c"].isspace()]
    if not chars:
        return
    top = max(s["size"] for s, _ in chars)
    radicals = [c["bbox"] for _, c in chars if c["c"] == "√"]
    # a radical's vinculum is the *highest* rule starting at its right edge; a
    # fraction bar right after the √ (\sqrt{\frac{a}{b}}) starts there too
    vinculums: set[int] = set()
    for r in radicals:
        cands = [b for b in bars if abs(r[2] - b.x0) < 0.5 * top and r[1] - 2 <= (b.y0 + b.y1) / 2 <= r[3] + 2]
        if cands:
            vinculums.add(id(min(cands, key=lambda b: b.y0)))

    def on_radical(bar: Rect, y: float) -> bool:
        return id(bar) in vinculums

    def xin(bar: Rect, c: dict) -> bool:
        return bar.x0 - 0.5 <= (c["bbox"][0] + c["bbox"][2]) / 2 <= bar.x1 + 0.5

    # 1. inline fractions (\tfrac{1}{s}): script-size glyphs hugging a short rule
    #    from above and below. Build the LaTeX now and park it in the first glyph
    #    (between RAW sentinels); innermost (narrowest) bars first.
    for bar in sorted(bars, key=lambda b: b.width):
        y = (bar.y0 + bar.y1) / 2
        if bar.width > 6 * top or on_radical(bar, y):
            continue
        live = [(s, c) for s, c in chars if c["c"] and xin(bar, c) and s["size"] < 0.92 * top]
        num = [(s, c) for s, c in live if 0 < y - c["origin"][1] < 0.6 * top]
        den = [(s, c) for s, c in live if 0 < c["origin"][1] - y < 1.1 * top]
        if not (num and den):
            continue
        tex = r"\frac{" + _chars_latex(num) + "}{" + _chars_latex(den) + "}"
        first = min(num + den, key=lambda sc: sc[1]["bbox"][0])[1]
        for _, c in num + den:
            c["c"] = ""
        first["c"] = RAW_OPEN + tex + RAW_CLOSE
    chars = [(s, c) for s, c in chars if c["c"]]

    for bar in bars:
        y = (bar.y0 + bar.y1) / 2
        if on_radical(bar, y):
            # the vinculum of a square root: bracket the radicand with sentinels that
            # texmap turns into \sqrt{...} braces (or parentheses in plain text)
            under = sorted((c for s, c in chars
                            if xin(bar, c) and 0 < c["origin"][1] - y < 1.6 * s["size"]),
                           key=lambda c: c["bbox"][0])
            if under and RAD_OPEN not in under[0]["c"]:
                under[0]["c"] = RAD_OPEN + under[0]["c"]
                under[-1]["c"] += RAD_CLOSE
            continue
        if bar.width > 3 * top:
            continue
        under, numerator = [], False
        for s, c in chars:
            x0, _, x1, _ = c["bbox"]
            if not bar.x0 - 0.5 <= (x0 + x1) / 2 <= bar.x1 + 0.5:
                continue
            oy = c["origin"][1]
            dy = (oy - y) / s["size"]  # how far below the bar this baseline sits, in em
            if 0.55 <= dy <= 1.3 and s["size"] >= 0.8 * top:
                under.append(c)
            elif dy < 0 and (y - oy) < 0.3 * s["size"]:
                numerator = True  # a baseline hugging the bar from above: it's a fraction
        if not under or numerator:
            continue
        width = max(c["bbox"][2] for c in under) - min(c["bbox"][0] for c in under)
        if bar.width <= 1.6 * width + 1.5:
            for c in under:
                if "̅" not in c["c"] and "̄" not in c["c"]:
                    c["c"] += "̅"
    for ln in lines:
        for s in ln["spans"]:
            s["text"] = "".join(c["c"] for c in s["chars"])


def _chars_latex(group: list) -> str:
    """LaTeX for a handful of glyphs (a fraction's numerator or denominator)."""
    from .inline import classify_tokens, line_tokens, math_run_latex  # (circular at import)
    spans, cur, src = [], None, None
    for s, c in sorted(group, key=lambda sc: sc[1]["bbox"][0]):
        if cur is None or s is not src:
            cur = {"text": "", "font": s["font"], "size": s["size"], "flags": s["flags"],
                   "origin": c["origin"], "bbox": c["bbox"], "chars": []}
            spans.append(cur)
            src = s
        cur["chars"].append(dict(c))
    for sp in spans:
        sp["text"] = "".join(c["c"] for c in sp["chars"])
        _refresh_geometry(sp)
    return math_run_latex(classify_tokens(line_tokens({"spans": spans})))


# --------------------------------------------------------------------------- #
# Glyph rows                                                                  #
# --------------------------------------------------------------------------- #
def _cy(c: dict) -> float:
    return (c["bbox"][1] + c["bbox"][3]) / 2


def glyph_rows(chars: list) -> tuple[list[dict], float]:
    """
    Group (span, char) pairs into visual rows (table cells). Rows are seeded by full-size text
    glyphs, clustered by glyph centre. Scripts and
    big CMEX delimiters -- whose origin is at the glyph *top* -- are attached to
    the row whose glyph centre is nearest, so 6^{+18}_{-17} and [GeV/c^2] stay put.
    """
    if not chars:
        return [], 10.0
    ext = lambda s: bool(EXT_FONT_RE.search(base_font(s["font"])))  # noqa: E731
    # main size = the biggest (non-CMEX) size with a fair share of the glyphs; not
    # the median -- in p^{lab}_{tag} the scripts outnumber the main glyphs
    counts = Counter(round(s["size"], 1) for s, _ in chars if not ext(s)) or \
        Counter(round(s["size"], 1) for s, _ in chars)
    total = sum(counts.values())
    size = max(z for z, n in counts.items() if n >= 0.15 * total)
    seeds = [(s, c) for s, c in chars if s["size"] >= 0.85 * size and not ext(s)]
    others = [(s, c) for s, c in chars if not (s["size"] >= 0.85 * size and not ext(s))]
    # cluster by glyph centre, not origin: a radical's origin is at its top
    rows: list[dict] = []
    for s, c in sorted(seeds, key=lambda sc: _cy(sc[1])):
        y = _cy(c)
        if rows and abs(y - rows[-1]["y"]) < 0.35 * size:
            rows[-1]["chars"].append((s, c))
        else:
            rows.append({"y": y, "chars": [(s, c)]})
    for r in rows:
        ys = sorted(c["origin"][1] for _, c in r["chars"])
        cys = sorted(_cy(c) for _, c in r["chars"])
        r["y"], r["cy"] = ys[len(ys) // 2], cys[len(cys) // 2]
    for s, c in others:
        near = min(rows, key=lambda r: abs(r["cy"] - _cy(c)), default=None)
        if near is not None and abs(near["cy"] - _cy(c)) < 0.9 * size:
            near["chars"].append((s, c))
        else:
            rows.append({"y": c["origin"][1], "cy": _cy(c), "chars": [(s, c)]})
    rows.sort(key=lambda r: r["y"])
    for r in rows:
        r["chars"].sort(key=lambda sc: sc[1]["bbox"][0])
    return rows, size


# --------------------------------------------------------------------------- #
# Fragment repair: stacked scripts split paragraphs and lines                 #
# --------------------------------------------------------------------------- #
def _line_main(ln: dict) -> tuple[float, float]:
    """(baseline y, size) of the line's biggest inked span. CMEX spans don't count:
    big delimiters/radicals hang from their top, so their origin isn't a baseline.
    Cached on the line (keyed by its span count): build_visual_lines asks for it O(n^2) times."""
    key = len(ln["spans"])
    if ln.get("_main_key") == key:
        return ln["_main"]
    ln["_main"], ln["_main_key"] = _line_main_uncached(ln), key
    return ln["_main"]


def _line_main_uncached(ln: dict) -> tuple[float, float]:
    best = None
    spans = [s for s in ln["spans"] if s["text"].strip()]
    if any(not EXT_FONT_RE.search(base_font(s["font"])) for s in spans):
        spans = [s for s in spans if not EXT_FONT_RE.search(base_font(s["font"]))]
    for s in spans:
        if not s["text"].strip():
            continue
        key = (round(s["size"], 1), len(s["text"].strip()))
        if best is None or key > best[0]:
            best = (key, s)
    if best is None:
        return ln["bbox"][3], 1.0
    return best[1]["origin"][1], best[1]["size"]


def _is_fragment(ln: dict, ref_size: float) -> bool:
    """A line made only of script-size glyphs, accent marks, or one big radical/operator."""
    inked = [s for s in ln["spans"] if s["text"].strip()]
    if not inked:
        return True
    if all(set(s["text"].strip()) <= set(ACCENT_GLYPHS) for s in inked):
        return True
    text = "".join(s["text"].strip() for s in inked)
    if len(text) <= 2 and (set(text) <= set("√∑∏∫∮⋃⋂") or all(EXT_FONT_RE.search(base_font(s["font"])) for s in inked)):
        return True  # big glyphs hang below the baseline, so MuPDF gives them their own line
    return max(s["size"] for s in inked) < 0.85 * ref_size and len("".join(s["text"] for s in inked)) <= 16


def _same_visual_line(a: dict, b: dict) -> bool:
    ya, sa = _line_main(a)
    yb, sb = _line_main(b)
    s = max(sa, sb)
    if abs(sa - sb) > 0.25 * s or abs(ya - yb) > 0.3 * s:
        return False
    gap = max(b["bbox"][0] - a["bbox"][2], a["bbox"][0] - b["bbox"][2])
    return gap < 0.9 * s


def _hangs_on(frag: dict, ln: dict, ref_size: float) -> float | None:
    """If `frag` (script/accent/radical) can hang off `ln`, return a cost (lower = better)."""
    fy0, fy1 = frag["bbox"][1], frag["bbox"][3]
    ly0, ly1 = ln["bbox"][1], ln["bbox"][3]
    overlap = min(fy1, ly1) - max(fy0, ly0)
    if overlap <= 0:
        return None
    if frag["bbox"][0] < ln["bbox"][0] - ref_size or frag["bbox"][2] > ln["bbox"][2] + 2 * ref_size:
        return None
    cost = -overlap / max(0.1, fy1 - fy0) + 0.01 * abs(_line_main(frag)[0] - _line_main(ln)[0])
    # a tall radical overlaps the line above too; it belongs to the line whose
    # glyphs (its radicand) start right after it
    if "√" in "".join(s["text"] for s in frag["spans"]):
        x1 = frag["bbox"][2]
        if any(-1 <= c["bbox"][0] - x1 <= ref_size and fy0 <= c["origin"][1] <= fy1 + 2
               for s in ln["spans"] for c in s["chars"] if not c["c"].isspace()):
            cost -= 1.0
    # most vertical overlap wins; baseline distance breaks ties
    return cost


def _merge_line_group(group: list[dict]) -> dict:
    if len(group) == 1:
        return group[0]
    spans = sorted((s for ln in group for s in ln["spans"]), key=lambda s: s["bbox"][0])
    line = {"bbox": tuple(union_rects(Rect(ln["bbox"]) for ln in group)), "dir": (1.0, 0.0),
            "spans": spans}
    attach_accents(line)  # an accent and its letter may have arrived in different pieces
    return line


def build_visual_lines(lines: list, ref_size: float) -> list:
    """Merge lines that share a baseline, and hang script-only fragments on their line."""
    horiz = [ln for ln in lines if is_horizontal(ln)]
    other = [ln for ln in lines if not is_horizontal(ln)]
    mains = [ln for ln in horiz if not _is_fragment(ln, ref_size)]
    frags = [ln for ln in horiz if _is_fragment(ln, ref_size)]
    if not mains:
        mains, frags = frags, []
    groups: list[list[dict]] = []
    for ln in sorted(mains, key=lambda l: (_line_main(l)[0], l["bbox"][0])):
        for g in groups:
            if any(_same_visual_line(m, ln) for m in g):
                g.append(ln)
                break
        else:
            groups.append([ln])
    for fr in frags:
        best, best_d = None, None
        for g in groups:
            for m in g:
                d = _hangs_on(fr, m, ref_size)
                if d is not None and (best_d is None or d < best_d):
                    best, best_d = g, d
        if best is not None:
            best.append(fr)
        else:
            groups.append([fr])
    merged = [_merge_line_group(g) for g in groups]
    merged.sort(key=lambda l: (_line_main(l)[0], l["bbox"][0]))
    return merged + other


def _dominant_size(lines: list) -> float:
    sizes = Counter()
    for ln in lines:
        for s in ln["spans"]:
            sizes[round(s["size"], 1)] += len(s["text"].strip())
    return sizes.most_common(1)[0][0] if sizes else 10.0


def _should_merge(a: BlockInfo, b: BlockInfo) -> bool:
    if a.rotated or b.rotated:
        return False
    ref = max(a.size, b.size)
    x = (a.rect + (-0.5, -0.5, 0.5, 0.5)) & (b.rect + (-0.5, -0.5, 0.5, 0.5))
    if x.is_empty or x.height < 1.0:
        # side by side on one text line, a couple of points apart ("s/4 −" | "|p*|²")
        vov = min(a.rect.y1, b.rect.y1) - max(a.rect.y0, b.rect.y0)
        hgap = max(b.rect.x0 - a.rect.x1, a.rect.x0 - b.rect.x1)
        if not (vov > 0.5 * min(a.rect.height, b.rect.height) and hgap < 1.0 * ref):
            return False
    for la in a.lines:
        for lb in b.lines:
            if _same_visual_line(la, lb):
                return True

    def best(fr: dict, lines: list) -> float | None:
        costs = [c for m in lines if m is not fr and (c := _hangs_on(fr, m, ref)) is not None]
        return min(costs) if costs else None

    # A fragment (script, accent, big radical) that hangs better on a line of the
    # other block than on any line of its own: the blocks are one paragraph.
    for small, big in ((a, b), (b, a)):
        mains = [ln for ln in small.lines if not _is_fragment(ln, ref)]
        for fr in small.lines:
            if not _is_fragment(fr, ref):
                continue
            other = best(fr, big.lines)
            if other is None:
                continue
            own = best(fr, mains)
            if own is None or other < own:
                return True
    return False


def merge_infos(a: BlockInfo, b: BlockInfo) -> BlockInfo:
    lines = a.lines + b.lines
    return analyze_block(build_visual_lines(lines, _dominant_size(lines)))  # type: ignore[return-value]


def _wide_gaps(ln: dict, width: float) -> int:
    xs = sorted((c["bbox"][0], c["bbox"][2]) for s in ln["spans"] for c in s["chars"]
                if not c["c"].isspace())
    n, right = 0, None
    for x0, x1 in xs:
        if right is not None and x0 - right > width:
            n += 1
        right = x1 if right is None else max(right, x1)
    return n


def caption_group(cap: BlockInfo, infos: list[BlockInfo], rules: list[Rect] = ()) -> list[BlockInfo]:
    """
    MuPDF often cuts a long caption into several blocks. Collect the blocks that
    continue it: directly below (gap < 0.7 em), same font size, same column, no
    rule in between, and no table-like gaps inside their lines.
    """
    group, bottom = [cap], cap.rect.y1
    for i in sorted(infos, key=lambda i: i.rect.y0):
        if i is cap or i.rotated or i.rect.y0 < cap.rect.y0 + 1:
            continue
        overlap = min(i.rect.x1, cap.rect.x1) - max(i.rect.x0, cap.rect.x0)
        if overlap < 0.8 * min(i.rect.width, cap.rect.width):
            continue
        if i.rect.y0 > bottom + 0.7 * cap.size or i.caption or abs(i.size - cap.size) > 0.3:
            break
        if any(bottom - 1 <= (r.y0 + r.y1) / 2 <= i.rect.y0 + 1 and r.width >= 0.5 * cap.rect.width
               and min(r.x1, cap.rect.x1) - max(r.x0, cap.rect.x0) > 0 for r in rules):
            break
        # a table row, not caption text: several cell gaps on one line (prose has at
        # most an odd wide gap around inline math)
        if any(_wide_gaps(ln, 0.8 * i.size) >= 2 for ln in i.lines):
            break
        # caption text is justified to the caption's left edge; table rows and
        # headers below it are indented. A tiny fragment (the "√" of inline math)
        # is part of a caption line, wherever it starts.
        tiny = len(i.text.strip()) <= 3 and i.rect.width < 2 * i.size
        if abs(i.rect.x0 - cap.rect.x0) > 2.0 and not tiny:
            break
        group.append(i)
        bottom = max(bottom, i.rect.y1)
    return group


def merge_fragments(infos: list[BlockInfo]) -> list[BlockInfo]:
    """Glue the overlapping blocks MuPDF cuts out of one paragraph back together."""
    infos = list(infos)
    changed = True
    while changed:
        changed = False
        infos.sort(key=lambda i: (i.rect.y0, i.rect.x0))
        for k, a in enumerate(infos):
            for b in infos[k + 1:]:
                if b.rect.y0 > a.rect.y1 + 1:
                    break
                if _should_merge(a, b):
                    infos.remove(a)
                    infos.remove(b)
                    infos.append(merge_infos(a, b))
                    changed = True
                    break
            if changed:
                break
    # lines inside a single block can be fragmented the same way
    out = []
    for i in infos:
        if i.rotated or i.n_lines < 2:
            out.append(i)
            continue
        vis = build_visual_lines(i.lines, i.size)
        out.append(analyze_block(vis) if len(vis) != len(i.lines) else i)
    return [i for i in out if i is not None]
