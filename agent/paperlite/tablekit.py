"""
tablekit - caption-anchored table extraction on papertools' own text model.

Why not page.find_tables()? Its cells come from MuPDF's plain-text words, so
math italics come back as "_x_", raw CMEX codes leak through, stacked
uncertainties (6^{+18}_{-17}) land on separate rows, and words get cut at
guessed column edges. Here every cell is rendered by the same inline
formatter as the body text.

Pipeline per "Table N" caption
    1. region    follow the chain of horizontal rules under (else above) the
                 caption until prose, another caption or a figure stops it
    2. rows      cluster full-size glyphs by baseline; hang smaller script glyphs
                 on the nearest row (stacked uncertainties stay in one cell)
    3. chunks    split each row at gaps wider than ~0.75 em or at vertical rules
    4. columns   whitespace gutters shared by the body rows, plus vertical rules
    5. header    rows above the first full-width rule below the top rule;
                 header cells covering several columns keep their span
    6. export    Markdown / CSV / LaTeX / JSON, plus a PNG crop and QA flags
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from .core import (
    TABLE_CAPTION_RE, Rect, inside_frac, looks_like_prose, pymupdf,
)
from .inline import block_tokens, line_tokens, render_tokens
from .textblocks import (
    BlockInfo, _refresh_geometry, caption_group, glyph_rows as group_rows, mark_overlines,
    merge_infos,
)

GAP_EM = 0.75   # a horizontal gap wider than this (in em) separates cells


# --------------------------------------------------------------------------- #
# Data model                                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class Cell:
    text: str
    col: int
    span: int = 1


@dataclass
class Table:
    page: int                      # 1-based
    label: str                     # "Table II"
    caption: str                   # rendered caption Markdown
    rect: Rect
    ncols: int = 0
    header: list = field(default_factory=list)   # list[list[Cell]]
    rows: list = field(default_factory=list)     # list[list[str]], ncols each
    n_hrules: int = 0
    n_vrules: int = 0
    flags: list = field(default_factory=list)
    stem: str = ""                 # file stem, e.g. "table_2_p6"

    # ---- header helpers -------------------------------------------------- #
    def header_flat(self) -> list[str]:
        """One label per column; a spanning group label prefixes each sub-column."""
        cols = [[] for _ in range(self.ncols)]
        for row in self.header:
            for c in row:
                for k in range(c.col, min(self.ncols, c.col + c.span)):
                    if c.text:
                        cols[k].append(c.text)
        return [" ".join(parts) for parts in cols]

    # ---- exporters ------------------------------------------------------- #
    def to_markdown(self) -> str:
        head = self.header_flat() if self.header else [f"Col{k + 1}" for k in range(self.ncols)]
        lines = ["| " + " | ".join(_md_cell(h) for h in head) + " |",
                 "|" + "|".join("---" for _ in head) + "|"]
        for r in self.rows:
            lines.append("| " + " | ".join(_md_cell(c) for c in r) + " |")
        return "\n".join(lines)

    def to_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        if self.header:
            w.writerow(self.header_flat())
        w.writerows(self.rows)
        return buf.getvalue()

    def to_latex(self) -> str:
        spec = "l" + "c" * (self.ncols - 1)
        out = [r"\begin{table}[ht]", r"\centering",
               r"\caption{" + _strip_md(self.caption) + "}", r"\begin{tabular}{" + spec + "}",
               r"\hline\hline"]
        for row in self.header:
            cells, k = [], 0
            for c in sorted(row, key=lambda c: c.col):
                cells += [""] * (c.col - k)
                t = _strip_md(c.text)
                cells.append(rf"\multicolumn{{{c.span}}}{{c}}{{{t}}}" if c.span > 1 else t)
                k = c.col + c.span
            cells += [""] * (self.ncols - k)
            out.append(" & ".join(cells) + r" \\")
        if self.header:
            out.append(r"\hline")
        for r in self.rows:
            out.append(" & ".join(_strip_md(c) for c in r) + r" \\")
        out += [r"\hline\hline", r"\end{tabular}", r"\end{table}"]
        return "\n".join(out)

    def to_dict(self) -> dict:
        return {
            "label": self.label, "page": self.page, "caption": self.caption,
            "bbox_pt": [round(v, 1) for v in self.rect], "ncols": self.ncols,
            "header": [[{"text": c.text, "col": c.col, "span": c.span} for c in r] for r in self.header],
            "rows": self.rows, "hrules": self.n_hrules, "vrules": self.n_vrules,
            "flags": self.flags, "files": {ext: f"tables/{self.stem}.{ext}"
                                           for ext in ("md", "csv", "tex", "png")} if self.stem else {},
        }


def _md_cell(text: str) -> str:
    """Escape pipes for GFM tables: \\vert inside $math$, \\| outside."""
    parts = re.split(r"((?<!\\)\$[^$]*(?<!\\)\$)", text.replace("\n", " "))
    out = []
    for k, p in enumerate(parts):
        if k % 2:
            out.append(p.replace(r"\|", r"\Vert ").replace("|", r"\vert "))
        else:
            out.append(p.replace("|", r"\|"))
    return "".join(out).strip()


def _strip_md(text: str) -> str:
    """Markdown cell -> LaTeX cell text: drop **/* emphasis, map <sup>/<sub>."""
    t = re.sub(r"<sup>(.*?)</sup>", r"$^{\1}$", text)
    t = re.sub(r"<sub>(.*?)</sub>", r"$_{\1}$", t)
    parts = re.split(r"((?<!\\)\$[^$]*(?<!\\)\$)", t)
    return "".join(p if k % 2 else re.sub(r"(?<!\\)\*{1,3}", "", p).replace("%", r"\%")
                   .replace("&", r"\&") for k, p in enumerate(parts))


# --------------------------------------------------------------------------- #
# Geometry helpers                                                            #
# --------------------------------------------------------------------------- #
def vertical_rules(paths: list[tuple[dict, Rect]]) -> list[Rect]:
    out = []
    for d, _ in paths:
        for it in d.get("items", ()):
            if it[0] == "l":
                p, q = it[1], it[2]
                if abs(p.x - q.x) < 0.6 and abs(q.y - p.y) >= 6:
                    x = (p.x + q.x) / 2
                    out.append(Rect(x - 0.3, min(p.y, q.y), x + 0.3, max(p.y, q.y)))
            elif it[0] == "re":
                r = Rect(it[1])
                if r.width <= 1.6 and r.height >= 6:
                    out.append(r)
    return merge_vertical(out)


def merge_vertical(segs: list[Rect]) -> list[Rect]:
    """LaTeX draws a column rule as one short segment per row: stitch collinear
    segments (same x, gaps < 3pt) back into one rule."""
    out: list[Rect] = []
    for r in sorted(segs, key=lambda r: (round((r.x0 + r.x1) / 2), r.y0)):
        cx = (r.x0 + r.x1) / 2
        if out and abs((out[-1].x0 + out[-1].x1) / 2 - cx) < 0.6 and r.y0 - out[-1].y1 < 3.0:
            out[-1] = out[-1] | r
        else:
            out.append(Rect(r))
    return out


def _hoverlap(a: Rect, b: Rect) -> float:
    return min(a.x1, b.x1) - max(a.x0, b.x0)


def _line_max_gap(ln: dict) -> float:
    xs = sorted((c["bbox"][0], c["bbox"][2]) for s in ln["spans"] for c in s["chars"]
                if not c["c"].isspace())
    gap, right = 0.0, None
    for x0, x1 in xs:
        if right is not None:
            gap = max(gap, x0 - right)
        right = x1 if right is None else max(right, x1)
    return gap


def _is_stop(i: BlockInfo, body: float, ref_width: float) -> bool:
    """
    Something that cannot be part of a table: another caption, or running prose
    about as wide as the caption (a tall block of row labels can read like prose,
    but it is narrow and sits inside the table).
    """
    if i.caption:
        return True
    if i.rect.width < 0.75 * ref_width or not looks_like_prose(i.text, 12):
        return False
    # table cells: two lines side by side on one baseline never happens in prose
    base = sorted((ln["bbox"][3], ln["bbox"][0], ln["bbox"][2]) for ln in i.lines)
    for (ya, a0, a1), (yb, b0, b1) in zip(base, base[1:]):
        if abs(ya - yb) < 0.3 * i.size and (b0 > a1 or a0 > b1):
            return False
    return all(_line_max_gap(ln) < 1.0 * max(i.size, 1) for ln in i.lines)


# --------------------------------------------------------------------------- #
# 1. Region                                                                   #
# --------------------------------------------------------------------------- #
def _stop_in(i: BlockInfo, gap: Rect, body: float, ref_width: float) -> bool:
    """Is block `i` a stop sitting in the gap between two consecutive rules? A caption
    counts by its first line (MuPDF may have glued its own table into its block)."""
    if not _is_stop(i, body, ref_width):
        return False
    if i.caption and i.lines:
        first = Rect(i.lines[0]["bbox"])
        return gap.y0 - 1 <= (first.y0 + first.y1) / 2 <= gap.y1 + 1 and _hoverlap(first, gap) > 0
    return inside_frac(i.rect, gap + (0, -1, 0, 1)) > 0.5


def locate_region(cap: BlockInfo, hrules: list[Rect], infos: list[BlockInfo],
                  body: float, page_rect: Rect) -> tuple[Rect, list[Rect]] | None:
    for direction in ("below", "above"):
        got = _region_dir(cap, hrules, infos, body, direction)
        if got is not None:
            return got
    return None


def _region_dir(cap, hrules, infos, body, direction):
    c = cap.rect
    aligned = [r for r in hrules if _hoverlap(r, c) >= 0.5 * min(r.width, c.width)]
    # (the untrimmed original of a trimmed caption block is not a stop either)
    others = [i for i in infos if i is not cap and abs(i.rect.y0 - c.y0) > 1]
    if direction == "below":
        rules = sorted((r for r in aligned if r.y0 >= c.y1 - 2), key=lambda r: r.y0)
        if len(rules) < 2 or rules[0].y0 - c.y1 > 3 * body:
            return _rows_below(cap, others, aligned, body)  # 0-1 rules: no chain to follow
        chain = [rules[0]]
        for r in rules[1:]:
            gap = Rect(min(chain[0].x0, r.x0), chain[-1].y1, max(chain[0].x1, r.x1), r.y0)
            if any(_stop_in(i, gap, body, c.width) for i in others):
                break
            chain.append(r)
        if len(chain) < 2:  # only a header rule (later "rules" were fraction bars)
            return _rows_below(cap, others, aligned, body)
    else:
        rules = sorted((r for r in aligned if r.y1 <= c.y0 + 2), key=lambda r: -r.y1)
        if not rules or c.y0 - rules[0].y1 > 3 * body:
            return None
        chain = [rules[0]]
        for r in rules[1:]:
            gap = Rect(min(chain[0].x0, r.x0), r.y1, max(chain[0].x1, r.x1), chain[-1].y0)
            if any(_stop_in(i, gap, body, c.width) for i in others):
                break
            chain.append(r)
        chain.reverse()
    if len(chain) < 2:
        return None
    region = Rect(min(r.x0 for r in chain), chain[0].y0, max(r.x1 for r in chain), chain[-1].y1)
    if region.height < 1.5 * body:
        return None
    return region + (-1, -1, 1, 1), chain


def _rows_below(cap: BlockInfo, others: list[BlockInfo], rules: list[Rect],
                body: float) -> tuple[Rect, list[Rect]] | None:
    """
    Fallback for tables with fewer than two rules (only a header rule, or none):
    the blocks below the caption, until prose, another caption or a blank gap.
    Accepted only if it looks tabular -- several rows with cells side by side.
    """
    c = cap.rect
    below = sorted((i for i in others if i.rect.y0 >= c.y1 - 2
                    and _hoverlap(i.rect, c) > 0.3 * min(i.rect.width, c.width)), key=lambda i: i.rect.y0)
    region, bottom = None, c.y1
    for i in below:
        if i.rect.y0 - bottom > 1.5 * body or _is_stop(i, body, c.width):
            break
        region = Rect(i.rect) if region is None else region | i.rect
        bottom = max(bottom, i.rect.y1)
    if region is None:
        return None
    lines = [ln for i in below if region.contains(i.rect) for ln in i.lines]
    rows: dict[int, list] = {}
    for ln in lines:
        rows.setdefault(round(ln["bbox"][3] / 3), []).append(ln["bbox"])
    tabular = sum(1 for r in rows.values()
                  if any(b[0] > a[2] + 0.5 * body for a in r for b in r))
    if tabular < 2:
        return None
    return region + (-1, -1, 1, 1), [r for r in rules if region.intersects(r)]


# --------------------------------------------------------------------------- #
# 2-3. Rows and chunks                                                        #
# --------------------------------------------------------------------------- #
@dataclass
class Chunk:
    chars: list            # [(span, char)]
    x0: float
    x1: float
    y: float               # main baseline
    size: float

    def render(self) -> str:
        spans, cur, cur_src = [], None, None
        for s, c in sorted(self.chars, key=lambda sc: sc[1]["bbox"][0]):
            if cur is None or s is not cur_src:
                cur = {"text": "", "font": s["font"], "size": s["size"], "flags": s["flags"],
                       "origin": c["origin"], "bbox": c["bbox"], "chars": []}
                spans.append(cur)
                cur_src = s
            cur["chars"].append(c)
        for sp in spans:
            sp["text"] = "".join(c["c"] for c in sp["chars"])
            _refresh_geometry(sp)
        line = {"bbox": (self.x0, 0, self.x1, 0), "dir": (1.0, 0.0), "spans": spans}
        try:
            return render_tokens(line_tokens(line)).strip()
        except Exception:
            return "".join(c["c"] for _, c in self.chars).strip()


def _region_chars(region: Rect, infos: list[BlockInfo]) -> list:
    """Glyphs inside the region (MuPDF may have merged table rows into the caption block)."""
    out = []
    for i in infos:
        if i.rotated:
            continue
        if not region.intersects(i.rect):
            continue
        for ln in i.lines:
            for s in ln["spans"]:
                for c in s["chars"]:
                    if c["c"].isspace():
                        continue
                    x0, y0, x1, y1 = c["bbox"]
                    if region.x0 <= (x0 + x1) / 2 <= region.x1 and region.y0 <= c["origin"][1] - 0.3 * s["size"] <= region.y1:
                        out.append((s, c))
    return out


def _split_runs(chars: list, size: float, hard: list[float]) -> list[list]:
    chunks, cur, right = [], [], None
    for s, c in chars:
        x0, x1 = c["bbox"][0], c["bbox"][2]
        split = right is not None and (x0 - right > GAP_EM * size
                                       or any(right - 0.5 < vx < x0 + 0.5 for vx in hard))
        if split and cur:
            chunks.append(cur)
            cur = []
        cur.append((s, c))
        right = x1 if right is None or split else max(right, x1)
    if cur:
        chunks.append(cur)
    return chunks


def _split_at_gutter(chars: list, sx: float, size: float) -> list[list] | None:
    """Split a chunk that straddles gutter `sx` at its internal gap nearest to it."""
    best, best_d, right = None, None, None
    for k, (_, c) in enumerate(chars):
        x0, x1 = c["bbox"][0], c["bbox"][2]
        if right is not None and x0 - right > 0.12 * size:
            d = abs((x0 + right) / 2 - sx)
            if d < 2.0 * size and (best_d is None or d < best_d):
                best, best_d = k, d
        right = x1 if right is None else max(right, x1)
    return None if best is None else [chars[:best], chars[best:]]


def chunk_row(row: dict, size: float, hard: list[float], soft: list[float] = (),
              protected: list[Rect] = ()) -> list[Chunk]:
    """
    Split a row into cell chunks: at gaps wider than GAP_EM and at drawn
    vertical rules (`hard`). Then a chunk that straddles a body-column gutter
    (`soft`) is split at its gap nearest that gutter ("central value UL_obs
    UL_exp"), unless a partial rule under it (`protected`) marks a spanning cell.
    """
    chunks = _split_runs(row["chars"], size, hard)
    for sx in soft:
        out = []
        for ch in chunks:
            x0 = min(c["bbox"][0] for _, c in ch)
            x1 = max(c["bbox"][2] for _, c in ch)
            guarded = any(0 < r.y0 - row["y"] < 1.3 * size and min(r.x1, x1) - max(r.x0, x0) > 0
                          for r in protected)
            parts = _split_at_gutter(ch, sx, size) if (x0 + 1 < sx < x1 - 1 and not guarded) else None
            out.extend(parts or [ch])
        chunks = out
    return [Chunk(ch, min(c["bbox"][0] for _, c in ch), max(c["bbox"][2] for _, c in ch),
                  row["y"], size) for ch in chunks]


# --------------------------------------------------------------------------- #
# 4. Columns                                                                  #
# --------------------------------------------------------------------------- #
def find_columns(body_rows: list[list[Chunk]], all_rows: list[list[Chunk]], region: Rect,
                 vxs: list[float], size: float) -> list[tuple[float, float]]:
    x0, x1 = region.x0, region.x1
    step = 0.5
    n = int((x1 - x0) / step) + 1
    occ = [0] * n
    for row in body_rows:
        for ch in row:
            for k in range(max(0, int((ch.x0 - x0) / step)), min(n, int((ch.x1 - x0) / step) + 1)):
                occ[k] += 1
    allow = int(0.1 * len(body_rows))
    seps: list[float] = []
    k = 0
    inked = [i for i, v in enumerate(occ) if v > 0]
    if not inked:
        return [(x0, x1)]
    first, last = inked[0], inked[-1]
    k = first
    while k <= last:
        if occ[k] <= allow:
            j = k
            while j <= last and occ[j] <= allow:
                j += 1
            if (j - k) * step >= 0.3 * size:
                seps.append(x0 + (k + j) / 2 * step)
            k = j
        else:
            k += 1
    ruled = set()
    for vx in vxs:  # drawn column rules: always separators
        if x0 + 2 < vx < x1 - 2:
            near = [s for s in seps if abs(vx - s) <= 0.6 * size]
            for s in near:
                seps.remove(s)
            seps.append(vx)
            ruled.add(vx)
    seps.sort()

    def support(a: float, b: float) -> int:
        return sum(1 for row in body_rows if any(a <= (ch.x0 + ch.x1) / 2 <= b for ch in row))

    # a whitespace column backed by too few rows is an artefact (one wide cell
    # poking past a gutter): merge it into its neighbour
    need = max(2, -(-len(body_rows) * 15 // 100))
    changed = True
    while changed and seps:
        changed = False
        bounds = [x0] + seps + [x1]
        for k in range(len(bounds) - 1):
            if support(bounds[k], bounds[k + 1]) >= need:
                continue
            cand = [s for s in (bounds[k] if k > 0 else None,
                                bounds[k + 1] if k + 1 < len(bounds) - 1 else None)
                    if s is not None and s not in ruled]
            if cand:
                seps.remove(cand[0])
                changed = True
                break
    bounds = [x0] + seps + [x1]
    cols = [(a, b) for a, b in zip(bounds, bounds[1:]) if b - a > 1]
    used = [c for c in cols if any(_ov(ch, c) > 0 for row in all_rows for ch in row)]
    return used or [(x0, x1)]


def _ov(ch: Chunk, col: tuple[float, float]) -> float:
    return min(ch.x1, col[1]) - max(ch.x0, col[0])


def assign(chunk: Chunk, cols: list[tuple[float, float]],
           extent: tuple[float, float] | None = None) -> tuple[int, int]:
    """
    (first column, span). A chunk spans every column whose centre it covers; a
    header's true extent can be wider than its text (given by the \\cline under it).
    """
    x0, x1 = extent or (chunk.x0, chunk.x1)
    hits = [k for k, (a, b) in enumerate(cols) if x0 - 0.5 <= (a + b) / 2 <= x1 + 0.5]
    if not hits:
        best = max(range(len(cols)), key=lambda k: _ov(chunk, cols[k]))
        return best, 1
    return hits[0], hits[-1] - hits[0] + 1


def trim_caption(cap: BlockInfo, hrules: list[Rect]) -> BlockInfo:
    """MuPDF sometimes glues the table under a caption into the caption's block:
    keep only the lines above the first rule under the caption's first line."""
    from .textblocks import analyze_block
    if not cap.lines:
        return cap
    first_y1 = cap.lines[0]["bbox"][3]
    # a table rule spans most of the caption's width; a √ vinculum or fraction bar
    # inside the caption does not
    below = [r.y0 for r in hrules if r.y0 > first_y1 and _hoverlap(r, cap.rect) > 0.5 * r.width
             and r.y0 < cap.rect.y1 and r.width >= 0.5 * cap.rect.width]
    if not below:
        return cap
    keep = [ln for ln in cap.lines if (ln["bbox"][1] + ln["bbox"][3]) / 2 < min(below)]
    if not keep or len(keep) == len(cap.lines):
        return cap
    return analyze_block(keep) or cap


# --------------------------------------------------------------------------- #
# 5-6. Assemble                                                               #
# --------------------------------------------------------------------------- #
def _caption_md(cap: BlockInfo, bars: list[Rect]) -> str:
    """Caption text with its inline math (\\sqrt{...}, \\bar{B} from drawn rules)."""
    mark_overlines(cap.lines, [b for b in bars if (cap.rect + (-1, -4, 1, 1)).contains(b)])
    return render_tokens(block_tokens(cap.lines))


def caption_label(cap: BlockInfo) -> str:
    m = re.match(r"(tab(?:le)?\.?)\s*(\S+?)[.:|]?$", cap.caption.strip(), re.I)
    return f"Table {m.group(2)}" if m else cap.caption.strip(" .:")


def build_table(page: pymupdf.Page, pno: int, cap: BlockInfo, region: Rect, chain: list[Rect],
                infos: list[BlockInfo], hrules: list[Rect], vrules: list[Rect], body: float,
                bars: list[Rect] = ()) -> Table:
    tab = Table(page=pno + 1, label=caption_label(cap), rect=region,
                caption=_caption_md(cap, bars))
    inner_h = [r for r in hrules if region.contains(r) or inside_frac(r, region + (0, -1, 0, 1)) > 0.8]
    inner_v = [r for r in vrules if inside_frac(r, region) > 0.6 and r.height > 0.25 * region.height]
    vxs = sorted({round((r.x0 + r.x1) / 2, 1) for r in inner_v})
    tab.n_hrules, tab.n_vrules = len(inner_h), len(vxs)

    chars = _region_chars(region, infos)
    # drawn bars over letters inside cells (B B-bar pairs) -> combining overline
    short = [r for r in bars if r.width < 3 * body and region.contains(r)]
    if short:
        lines = [{"spans": [s]} for s in {id(s): s for s, _ in chars}.values()]
        mark_overlines(lines, short)
    row_chars, size = group_rows(chars)
    rows = [chunk_row(r, size, vxs) for r in row_chars]
    if not rows:
        tab.flags.append("empty")
        return tab

    # header / body split: the first wide rule below row 1 (top rules sit above
    # row 1; a table may have only that one header rule)
    wide = sorted(r.y0 for r in inner_h if r.width >= 0.85 * region.width)
    first_y = rows[0][0].y if rows[0] else region.y0
    cut = next((y for y in wide if y > first_y), None)
    n_head = sum(1 for r in rows if r and r[0].y < cut) if cut is not None else 1
    if n_head >= len(rows):
        n_head = 1
    head_rows, body_rows = rows[:n_head], rows[n_head:]

    cols = find_columns(body_rows or rows, rows, region, vxs, size)
    tab.ncols = len(cols)
    # re-split every row at the gutters the body rows agree on
    seps = [(a[1] + b[0]) / 2 for a, b in zip(cols, cols[1:])]
    partial = [r for r in inner_h if r.width < 0.85 * region.width]
    rows = [chunk_row(r, size, vxs, seps, partial) for r in row_chars]
    head_rows, body_rows = rows[:n_head], rows[n_head:]
    # header cells keep their spans; a partial rule (\cline / \cmidrule) just
    # under a header chunk gives its true extent
    for row in head_rows:
        cells: dict[int, Cell] = {}
        for ch in row:
            under = [r for r in partial if 0 < r.y0 - ch.y < 1.3 * ch.size
                     and min(r.x1, ch.x1) - max(r.x0, ch.x0) > 0]
            extent = (min([ch.x0] + [r.x0 for r in under]), max([ch.x1] + [r.x1 for r in under]))
            col, span = assign(ch, cols, extent)
            txt = ch.render()
            if col in cells:
                cells[col].text = (cells[col].text + " " + txt).strip()
                cells[col].span = max(cells[col].span, col + span - cells[col].col)
            else:
                cells[col] = Cell(txt, col, span)
        tab.header.append(sorted(cells.values(), key=lambda c: c.col))
    # body cells: spanning chunks go to their first column and are flagged
    spanning = False
    for row in body_rows:
        cells = [""] * tab.ncols
        for ch in row:
            col, span = assign(ch, cols)
            spanning |= span > 1
            cells[col] = (cells[col] + " " + ch.render()).strip()
        # a lowercase continuation of a wrapped first-column label
        if (tab.rows and cells[0][:1].islower() and not any(cells[1:])):
            tab.rows[-1][0] = (tab.rows[-1][0] + " " + cells[0]).strip()
            continue
        tab.rows.append(cells)

    if tab.ncols < 2:
        tab.flags.append("single-column")
    if spanning:
        tab.flags.append("spanning-body-cell")
    filled = sum(1 for r in tab.rows for c in r if c)
    if tab.rows and filled < 0.4 * len(tab.rows) * tab.ncols:
        tab.flags.append("sparse")
    if not tab.rows:
        tab.flags.append("no-body")
    return tab


def find_tables(page: pymupdf.Page, pno: int, infos: list[BlockInfo], bars: list[Rect],
                paths: list, body: float) -> list[Table]:
    """Every captioned table on the page."""
    caps = [i for i in infos if i.caption and TABLE_CAPTION_RE.match(i.caption)]
    if not caps:
        return []
    hrules = [b for b in bars if b.width >= 20]
    vrules = vertical_rules(paths)
    out = []
    claimed: list[Rect] = []
    for cap in sorted(caps, key=lambda i: i.rect.y0):
        cap = trim_caption(cap, hrules)
        group = caption_group(cap, infos, hrules)
        if len(group) > 1:  # a caption MuPDF split into several blocks
            for g in group[1:]:
                cap = merge_infos(cap, g)
        got = locate_region(cap, hrules, [i for i in infos if i not in group], body, page.rect)
        if got is None:
            out.append(Table(page=pno + 1, label=caption_label(cap), rect=Rect(cap.rect),
                             caption=_caption_md(cap, bars), flags=["no-region"]))
            continue
        region, chain = got
        if any(inside_frac(region, c) > 0.5 for c in claimed):
            continue
        claimed.append(region)
        out.append(build_table(page, pno, cap, region, chain, infos, hrules, vrules, body, bars))
    return out


# --------------------------------------------------------------------------- #
# Files                                                                       #
# --------------------------------------------------------------------------- #


