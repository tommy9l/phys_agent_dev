"""
Inline formatting: spans -> Markdown with $math$, <sup>/<sub>, **bold**, *italic*.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace

from .core import clean_text, is_roman, span_bold, span_italic, span_mono
from .textblocks import span_is_math
from .texmap import (
    GLUE_CHARS, RAW_OPEN, UNI2TEX, tex_join, to_latex, word_tex,
)


@dataclass
class Tok:
    text: str
    font: str
    size: float
    origin_y: float
    math: bool
    bold: bool
    italic: bool
    mono: bool
    x0: float = 0.0
    x1: float = 0.0
    role: str = "base"  # base | sup | sub
    kind: str = "T"     # T text | M math | G glue | S script | U unit
    unit: bool = False
    cite: bool = False


# Physical units that should live *inside* the math they qualify: "$1.0\,\text{GeV}/c^{2}$"
# rather than "$1.0$ GeV$/c^{2}$". Multi-letter only: "s", "K", "T" are too ambiguous.
UNIT_RE = re.compile(
    r"(?<![A-Za-z])((?:[kMGTP]?eV|[kMGT]?Hz|[afpnµμm]b|[cmnfµμk]m|[pnfµμm]s|mrad|ab)\b)"
    r"(?![A-Za-z])"
)


def split_units(toks: list[Tok]) -> list[Tok]:
    out: list[Tok] = []
    for k in toks:
        if k.math or k.mono or k.role != "base" or not UNIT_RE.search(k.text):
            out.append(k)
            continue
        pos = 0
        for m in UNIT_RE.finditer(k.text):
            if m.start() > pos:
                out.append(replace(k, text=k.text[pos:m.start()]))
            out.append(replace(k, text=m.group(1), unit=True))
            pos = m.end()
        if pos < len(k.text):
            out.append(replace(k, text=k.text[pos:]))
    return out


GLUE_WORD_MAX = 4  # "d", "dr", "CL", "UL", "BDT": short upright labels written into math


def split_glued_words(toks: list[Tok]) -> list[Tok]:
    """
    An upright word touching math with no space between ("CL" + "_s", "d" + "E",
    "|" + "d" + "z|") is part of that math, typeset in the text font. Split it off
    as kind W so it joins the math run as \\text{CL} / \\mathrm{d} instead of
    breaking it into "CL${}_{s}$" or "d$E/$d$x$". Repeats until stable, so glue
    chains outward from the math: in a Times-set "dE/dx" only "/" is a math glyph,
    yet "E", then "d", then "x" all join (italic letters become math letters).
    """
    while True:
        new = _glue_once(toks)
        if [t.kind for t in new] == [t.kind for t in toks]:
            return new
        toks = new


def _glue_once(toks: list[Tok]) -> list[Tok]:
    out: list[Tok] = []
    n = len(toks)
    for idx, k in enumerate(toks):
        if k.kind != "T" or k.mono:
            out.append(k)
            continue
        prev = toks[idx - 1] if idx > 0 else None
        nxt = toks[idx + 1] if idx + 1 < n else None
        glue_prev = (prev is not None and prev.kind in "MSW" and prev.text.strip()
                     and not prev.text.endswith(" ") and (k.text[:1].isalpha() or k.text.startswith("(")))
        glue_next = (nxt is not None and nxt.kind in "MSW" and nxt.text.strip()
                     and not k.text.endswith(" ") and not nxt.text.startswith(" ")
                     and (k.text[-1:].isalpha() or k.text.endswith(")")))
        text = k.text
        head = tail = ""
        if glue_next and (m := re.search(r"(?<![A-Za-z(])(?:[A-Za-z]{1,%d}|\([A-Za-z]{1,5}\.?\))$"
                                         % GLUE_WORD_MAX, text)):
            tail, text = m.group(0), text[: m.start()]
        if glue_prev and text and (m := re.match(r"(?:[A-Za-z]{1,%d}|\([A-Za-z]{1,5}\.?\))(?![A-Za-z])"
                                                  % GLUE_WORD_MAX, text)):
            head, text = m.group(0), text[m.end():]
        if head:
            out.append(replace(k, text=head, kind="W"))
        if text:
            out.append(replace(k, text=text))
        if tail:
            out.append(replace(k, text=tail, kind="W"))
    return out


def unit_tex(text: str) -> str:
    """'GeV' -> '\\text{GeV}', 'µm' -> '\\mu\\text{m}'."""
    t = text.strip()
    if t[:1] in "µμ":
        return r"\mu\text{" + t[1:] + "}"
    return r"\text{" + t + "}"


def line_tokens(line: dict) -> list[Tok]:
    """One token per span, with sub/superscript roles from baseline offsets."""
    toks = []
    for s in line["spans"]:
        t = clean_text(s["text"])
        if not t:
            continue
        toks.append(Tok(
            text=t, font=s["font"], size=s["size"], origin_y=s["origin"][1],
            math=span_is_math(s["font"], t), bold=span_bold(s),
            italic=span_italic(s), mono=span_mono(s), x0=s["bbox"][0], x1=s["bbox"][2],
        ))
        if s["flags"] & 1 and t.strip():  # MuPDF's own superscript flag
            toks[-1].role = "sup"
    if not toks:
        return toks
    frac = [k for k in toks if RAW_OPEN in k.text]  # inline \frac glyph: base-level math
    for k in frac:
        k.math = True
    # Reference = the biggest font on the line (ties: most text); scripts are
    # smaller and shifted. Size first, so a table cell "6^{+18}_{-17}" keeps "6"
    # as its base even though the scripts have more characters.
    ref = max(toks, key=lambda k: (round(k.size, 1), len(k.text.strip())))
    for k in toks:
        if not k.text.strip() or k.size >= ref.size * 0.88:
            if k.role == "sup" and k.size >= ref.size * 0.88:
                k.role = "base"
            continue
        dy = k.origin_y - ref.origin_y
        if dy < -0.12 * ref.size:
            k.role = "sup"
        elif dy > 0.08 * ref.size:
            k.role = "sub"
    for k in frac:
        k.role = "base"
    # A tiny glyph on the main baseline glued to a script is a script of that
    # script: in p_{e^+e^-} the 5pt "+" sits almost exactly on the main baseline.
    prev = None
    for k in toks:
        if not k.text.strip():
            continue
        if (prev is not None and k.role == "base" and prev.role != "base"
                and k.size < 0.88 * ref.size and k.size <= prev.size
                and k.x0 - prev.x1 < 0.3 * ref.size):
            k.role = prev.role
        prev = k
    # A visible gap between spans is a word space MuPDF didn't synthesise
    # ("e+e−" + "collider" -> "e+e− collider").
    for a, b in zip(toks, toks[1:]):
        if a.text.endswith(" ") or b.text.startswith(" ") or not a.text.strip() or not b.text.strip():
            continue
        if b.x0 - a.x1 > 0.22 * max(a.size, b.size) and b.role == "base":
            a.text += " "
    return toks


def block_tokens(lines: list) -> list[Tok]:
    """Concatenate the lines of a block, de-hyphenating line-final 'hyphen-' breaks."""
    from .core import join_hyphenated
    out: list[Tok] = []
    for line in lines:
        toks = line_tokens(line)
        if not toks:
            continue
        if out:
            k = len(out) - 1
            while k > 0 and not out[k].text.strip():
                k -= 1
            prev, nxt = out[k], toks[0]
            ptxt = prev.text.rstrip()
            if re.search(r"[A-Za-z]-$", ptxt) and re.match(r"\s*[A-Za-z]", nxt.text):
                lower = nxt.text.lstrip()[:1].islower()
                prev.text = join_hyphenated(ptxt, "") if lower else ptxt
                for t in out[k + 1:]:
                    t.text = ""
                nxt.text = nxt.text.lstrip()
            elif not out[-1].text.endswith((" ", "\t")):
                out[-1].text += " "
        out.extend(toks)
    return out


# Math-looking edges of a text span that touches a math span: "x" + " = 2, and"
PULL_AFTER = re.compile("^[\\s=+\\-−<>≤≥×/|)\\]0-9.′']+")
PULL_BEFORE = re.compile("[\\s=+\\-−<>≤≥×/|(\\[0-9.]+$")
BRACKET_GROUP = re.compile(r"^\s*[\[(][\d\s.,+\-−∞]*[\])]")  # "λ ∈" + "[0, 1]"


CITE_RE = re.compile(r"\[\d+(?:\s*[,–-]\s*\d+)*\]")  # [7], [8, 9], [44-46]
RELATIONS = tuple("=∈∉<>≤≥≈∼≃⊂⊆→")


def _after_relation(toks: list[Tok], idx: int) -> bool:
    """Does the nearest inked token before idx end with a relation (λ ∈ [0, 1])?"""
    for t in reversed(toks[:idx]):
        if t.text.strip():
            return t.text.rstrip().endswith(RELATIONS)
    return False


def split_citations(toks: list[Tok]) -> list[Tok]:
    """'[7]' / '[8, 9]' are citations -- prose, never part of a math run."""
    out: list[Tok] = []
    for k in toks:
        if k.math or k.role != "base" or not CITE_RE.search(k.text):
            out.append(k)
            continue
        pos = 0
        for m in CITE_RE.finditer(k.text):
            if m.start() > pos:
                out.append(replace(k, text=k.text[pos:m.start()]))
            out.append(replace(k, text=m.group(0), cite=True))
            pos = m.end()
        if pos < len(k.text):
            out.append(replace(k, text=k.text[pos:]))
    # hyperlinked citations arrive as separate spans: "[", "8", ", ", "9", "]"
    k = 0
    while k < len(out):
        t = out[k]
        if t.cite or t.math or not t.text.lstrip().startswith("[") or t.role != "base":
            k += 1
            continue
        buf, j = "", k
        while j < len(out) and len(buf) < 40 and not out[j].math:
            buf += out[j].text
            if "]" in out[j].text:
                break
            j += 1
        end = buf.find("]")
        if j < len(out) and end >= 0 and re.fullmatch(r"\s*" + CITE_RE.pattern, buf[: end + 1]):
            last = out[j]
            cut = last.text.index("]") + 1
            if cut < len(last.text):  # "], which" -> "]" + ", which"
                out[j:j + 1] = [replace(last, text=last.text[:cut]), replace(last, text=last.text[cut:])]
            for t2 in out[k:j + 1]:
                t2.cite = True
            k = j + 1
        else:
            k += 1
    return out


def _symbols_only(s: str) -> bool:
    """Greek letters / math symbols set in a text font ("Υ", a superscript "◦")."""
    core = [c for c in s if not unicodedata.combining(c)]
    return bool(core) and all(c in UNI2TEX for c in core)


# Set per document by pipeline.open_paper(): True when the paper sets math
# variables in the *text* italic font (Times/newtx papers), so a lone italic
# letter is math. Off for Computer Modern papers, whose variables are in CMMI and
# whose lone italic letters are journal letters ("Phys. Rev. D").
TEXT_ITALIC_MATH = True


def detect_text_italic_math(all_infos: list) -> bool:
    math_letters = lone_italic = 0
    for infos in all_infos:
        for i in infos:
            for ln in i.lines:
                for sp in ln["spans"]:
                    t = sp["text"].strip()
                    if not is_roman(sp["font"]):
                        math_letters += sum(c.isascii() and c.isalpha() for c in t)
                    elif span_italic(sp) and re.fullmatch("[A-Za-z]", t):
                        lone_italic += 1
    return lone_italic >= max(3, 0.25 * math_letters)


def _italic_prose_next_to(toks: list[Tok], idx: int) -> bool:
    """Is the nearest inked token on either side an italic *word* (a title set in
    italics word by word, e.g. "Mesons in a Relativized ...", "Phys. Rev. D")?"""
    for rng in (range(idx - 1, -1, -1), range(idx + 1, len(toks))):
        for j in rng:
            t = toks[j]
            if t.text.strip():
                # an italic word, or abbreviation ("J." in "Eur. Phys. J. C"), not another lone letter
                if t.italic and not t.math and re.search(r"[A-Za-z]", t.text) and len(t.text.strip()) >= 2:
                    return True
                break
    return False


def classify_tokens(toks: list[Tok]) -> list[Tok]:
    toks = split_citations(split_units(toks))
    for idx, k in enumerate(toks):
        s = k.text.strip()
        if k.cite:
            k.kind = "T"
        elif k.unit:
            k.kind = "U"
        elif k.math or _symbols_only(s) or (
                TEXT_ITALIC_MATH and k.italic and not k.mono and k.role == "base"
                and re.fullmatch("[A-Za-z][\u0300-\u036f\u20d7]*", s)
                and not _italic_prose_next_to(toks, idx)):
            # a lone italic letter is a math variable: Times-based papers set math
            # italic in the *text* italic font ("B mesons", "Υ(4S)", "the z axis")
            k.math = True
            k.kind = "M"
        elif not s:
            k.kind = "G"  # whitespace never breaks a math run
        elif k.role != "base" and len(s) <= 12 and not k.mono:
            k.kind = "S"
        elif s and all(c in GLUE_CHARS for c in s):
            k.kind = "G"
        else:
            k.kind = "T"
    toks = split_glued_words(toks)
    out: list[Tok] = []
    for idx, k in enumerate(toks):
        prev_m = idx > 0 and toks[idx - 1].kind in "MUS"
        next_m = idx + 1 < len(toks) and toks[idx + 1].kind in "MUS"
        if k.cite and _after_relation(toks, idx):
            k.kind = "G"  # "λ ∈ [0, 1]": an interval after a relation, not a citation
        if k.kind != "T" or k.cite or not (prev_m or next_m):
            out.append(k)
            continue
        text, head, tail = k.text, "", ""
        bracket = BRACKET_GROUP.match(text) if _after_relation(toks, idx) else None
        if prev_m and (m := bracket or PULL_AFTER.match(text)):
            cand = m.group(0)
            if not cand.rstrip().endswith(("]", ")")):
                cand = cand.rstrip(" .")  # never swallow a sentence-ending period
            if cand.endswith("-") and text[len(cand):][:1].isalpha():
                cand = cand[:-1]  # "x-coordinate": the hyphen belongs to the word
            if cand.strip():
                head, text = cand, text[len(cand):]
        if next_m and (m := PULL_BEFORE.search(text)):
            cand = m.group(0).lstrip(" .")
            if cand.startswith("-") and re.search(r"[A-Za-z]$", text[: len(text) - len(cand)]):
                cand = ""  # "anti-" + "b": a word hyphen, not a minus sign
            if cand.strip() and not re.fullmatch(r"[\d.]+\s+", cand):
                tail, text = cand, text[: len(text) - len(cand)]
        if head:
            out.append(replace(k, text=head, kind="G"))
        if text:
            rest = replace(k, text=text)
            if _symbols_only(text.strip()):  # "Υ" left over from "Υ(1"
                rest.math, rest.kind = True, "M"
            out.append(rest)
        if tail:
            out.append(replace(k, text=tail, kind="G"))
    return out


def _script_latex(toks: list[Tok]) -> str:
    """
    One script group. A glyph clearly smaller than the group's first glyph is a
    second-level script of the glyph before it: M_{X_{s}}, not M_{Xs}.
    """
    toks = _join_upright(toks)
    first = toks[0].size
    pieces: list[str] = []
    k = 0
    while k < len(toks):
        t = toks[k]
        if t.size < 0.85 * first and pieces:
            role = "_" if t.origin_y > toks[0].origin_y else "^"
            inner = []
            while k < len(toks) and toks[k].size < 0.85 * first:
                inner.append(_tok_tex(toks[k], script=True))
                k += 1
            pieces.append(role + "{" + tex_join(inner) + "}")
            continue
        pieces.append(_tok_tex(t, script=True))
        k += 1
    return tex_join(pieces)


def _is_upright_word(t: Tok) -> bool:
    """Upright text-font letters inside math. Italic text-font letters are math
    variables (Times-based papers set math italic in the text italic font)."""
    s = t.text.strip()
    if t.italic:
        return False
    return t.kind == "W" or (s.isalpha() and s.isascii() and is_roman(t.font) and not t.math)


def _join_upright(toks: list[Tok]) -> list[Tok]:
    """A kerned script word can arrive as several spans ("tr" + "ue"): rejoin them,
    or "tr" alone would read as the trace operator."""
    out: list[Tok] = []
    for t in toks:
        p = out[-1] if out else None
        if (p is not None and _is_upright_word(p) and _is_upright_word(t)
                and abs(p.size - t.size) < 0.3 and abs(p.origin_y - t.origin_y) < 0.5
                and not p.text.endswith(" ") and not t.text.startswith(" ")
                and t.x0 - p.x1 < 0.15 * t.size):
            out[-1] = replace(p, text=p.text + t.text, x1=t.x1)
        else:
            out.append(t)
    return out


def _tok_tex(t: Tok, script: bool = False) -> str:
    """Upright (text-font) words inside math become \\text{...}: N_{\\text{sig}}.
    Only base-level words may be operators: \\max, but UL_{\\text{exp}}, not UL_{\\exp}."""
    if _is_upright_word(t):
        return word_tex(t.text, func=not script)
    return to_latex(t.text, t.font)


def math_run_latex(toks: list[Tok]) -> str:
    """
    LaTeX for a run of inline-math tokens: base text + one ^{...} and one _{...}
    per base. Stacked scripts (M^{true}_{X_s}) arrive interleaved by x position,
    so all script tokens up to the next base glyph are split by role first --
    otherwise "M _{X} ^{true} _{s}" is a TeX double-subscript error.
    """
    pieces: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.role == "base":
            if t.unit:  # thin space between a number and its unit: 4.18\,\text{GeV}
                sep = r"\," if re.search(r"[\d.)]$", "".join(pieces).rstrip()) else ""
                pieces.append(sep + unit_tex(t.text))
            else:
                pieces.append(_tok_tex(t))
            i += 1
            continue
        grp = []
        while i < len(toks) and (toks[i].role != "base" or not toks[i].text.strip()):
            if toks[i].text.strip():
                grp.append(toks[i])
            i += 1
        if not "".join(pieces).strip():
            pieces.append("{}")
        for role, mark in (("sub", "_"), ("sup", "^")):
            part = [x for x in grp if x.role == role]
            if part:
                pieces.append(mark + "{" + _script_latex(part) + "}")
    return tex_join(pieces)


def render_inline_math(toks: list[Tok]) -> str:
    raw = "".join(t.text for t in toks)
    lead = raw[: len(raw) - len(raw.lstrip())]
    trail = raw[len(raw.rstrip()):]
    body = math_run_latex(toks)
    if m := re.match(r"[,.;:]+\s*", body):  # a run never starts with punctuation
        lead, body = lead + m.group(0), body[m.end():]
    punct = ""
    pairs = {")": "(", "]": "[", "(": ")", "[": "]"}
    changed = True
    while body and changed:
        changed = False
        if m := re.search(r"[.,;:]+$", body):  # sentence punctuation stays outside $...$
            punct, body, changed = m.group(0) + punct, body[: m.start()].rstrip(), True
        if body and body[-1] in ")]" and body.count(body[-1]) > body.count(pairs[body[-1]]):
            punct, body, changed = body[-1] + punct, body[:-1].rstrip(), True
        if body and body[0] in "([" and body.count(body[0]) > body.count(pairs[body[0]]):
            lead, body, changed = lead + body[0], body[1:].lstrip(), True
    if not body:
        return raw
    return f"{lead}${body}${punct}{trail}"


def wrap_style(style: tuple, text: str) -> str:
    """Plain-text runs are kept as they are: no **bold**/*italic*/`code` markers in line text."""
    return text


def render_plain(toks: list[Tok]) -> str:
    """Group same-style tokens so emphasis markers wrap whole phrases, not each word."""
    segs: list[list] = []
    for t in toks:
        if t.role != "base" and t.text.strip():
            tag = "sup" if t.role == "sup" else "sub"
            segs.append([("script",), f"<{tag}>{t.text.strip()}</{tag}>"])
            continue
        style = ("code",) if t.mono else (t.bold, t.italic)
        if segs and (segs[-1][0] == style or (not t.text.strip() and segs[-1][0] != ("script",))):
            segs[-1][1] += t.text
        else:
            segs.append([style, t.text])
    return "".join(wrap_style(style, text) for style, text in segs)


def render_tokens(toks: list[Tok]) -> str:
    """Split tokens into math runs ($...$) and plain-text runs, then render both."""
    toks = classify_tokens(toks)
    out, plain, i, n = [], [], 0, len(toks)
    while i < n:
        if toks[i].kind in "MGSUW":
            j = i
            while j < n and toks[j].kind in "MGSUW":
                j += 1
            kinds = {toks[k].kind for k in range(i, j)}
            # math if it has a math glyph, or a unit/upright word with a script
            # ("fb^{-1}", "UL_obs"); "60 MeV below" alone stays plain text
            if "M" in kinds or ("S" in kinds and kinds & {"U", "W"}):
                if plain:
                    out.append(render_plain(plain))
                    plain = []
                out.append(render_inline_math(toks[i:j]))
            else:
                plain.extend(toks[i:j])
            i = j
        else:
            plain.append(toks[i])
            i += 1
    if plain:
        out.append(render_plain(plain))
    md = re.sub(r"[ \t]+", " ", "".join(out)).strip()
    # Two inline-math runs back to back ("$\pi^{-}$$\Upsilon$") would read as a
    # display-math delimiter: fuse them into one run.
    md = re.sub(r"(?<!\\)\$\$", " ", md)
    # "$B^{+}\to$ $\bar{D}^{0}$": two runs with nothing but a space between are one
    return re.sub(r"(?<=[^\s$\\])\$ \$(?=[^\s$])", " ", md)
