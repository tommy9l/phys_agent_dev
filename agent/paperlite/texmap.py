"""
Unicode -> LaTeX. Characters arrive here already NFC/NFD-normalised by
textblocks.attach_accents, which turns a TeX accent glyph sitting over a letter
("ν" + "¯") into a combining mark ("ν̄") so it renders as \\bar{\\nu}.
"""
from __future__ import annotations

import re
import unicodedata

from .core import base_font

GREEK = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta", "ϵ": r"\epsilon",
    "ε": r"\varepsilon", "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta", "ϑ": r"\vartheta",
    "ι": r"\iota", "κ": r"\kappa", "λ": r"\lambda", "μ": r"\mu", "µ": r"\mu", "ν": r"\nu",
    "ξ": r"\xi", "π": r"\pi", "ϖ": r"\varpi", "ρ": r"\rho", "ϱ": r"\varrho", "σ": r"\sigma",
    "ς": r"\varsigma", "τ": r"\tau", "υ": r"\upsilon", "ϕ": r"\phi", "φ": r"\varphi",
    "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega", "Γ": r"\Gamma", "Δ": r"\Delta",
    "Θ": r"\Theta", "Λ": r"\Lambda", "Ξ": r"\Xi", "Π": r"\Pi", "Σ": r"\Sigma",
    "Υ": r"\Upsilon", "Φ": r"\Phi", "Ψ": r"\Psi", "Ω": r"\Omega",
}
SYMBOLS = {
    # big operators
    "∑": r"\sum", "∏": r"\prod", "∐": r"\coprod", "∫": r"\int", "∬": r"\iint",
    "∮": r"\oint", "⋃": r"\bigcup", "⋂": r"\bigcap", "⨁": r"\bigoplus", "⨂": r"\bigotimes",
    "√": r"\sqrt",
    # letter-like
    "∂": r"\partial", "∇": r"\nabla", "∞": r"\infty", "ℏ": r"\hbar", "ℓ": r"\ell",
    "∆": r"\Delta", "Ω": r"\Omega",  # U+2206 INCREMENT / U+2126 OHM look-alikes
    "∅": r"\emptyset", "∀": r"\forall", "∃": r"\exists", "¬": r"\neg",
    "ℝ": r"\mathbb{R}", "ℕ": r"\mathbb{N}", "ℤ": r"\mathbb{Z}", "ℚ": r"\mathbb{Q}",
    "ℂ": r"\mathbb{C}", "ℒ": r"\mathcal{L}", "ℋ": r"\mathcal{H}",
    # relations
    "≤": r"\leq", "≥": r"\geq", "≠": r"\neq", "≈": r"\approx", "≃": r"\simeq",
    "≅": r"\cong", "∼": r"\sim", "≡": r"\equiv", "∝": r"\propto", "≪": r"\ll",
    "≫": r"\gg", "⊥": r"\perp", "∥": r"\parallel", "∈": r"\in", "∉": r"\notin",
    "∋": r"\ni", "⊂": r"\subset", "⊆": r"\subseteq", "⊃": r"\supset", "⊇": r"\supseteq",
    "≲": r"\lesssim", "≳": r"\gtrsim",
    # binary operators
    "±": r"\pm", "∓": r"\mp", "×": r"\times", "÷": r"\div", "·": r"\cdot", "⋅": r"\cdot",
    "∘": r"\circ", "∗": "*", "⊗": r"\otimes", "⊕": r"\oplus", "†": r"\dagger",
    "‡": r"\ddagger", "⋆": r"\star", "∪": r"\cup", "∩": r"\cap", "∧": r"\wedge",
    "∨": r"\vee", "∖": r"\setminus", "−": "-",
    # arrows & delimiters
    "→": r"\to", "←": r"\leftarrow", "↔": r"\leftrightarrow", "⇒": r"\Rightarrow",
    "⇐": r"\Leftarrow", "⇔": r"\Leftrightarrow", "↦": r"\mapsto", "↑": r"\uparrow",
    "↓": r"\downarrow", "⟨": r"\langle", "⟩": r"\rangle", "〈": r"\langle",
    "〉": r"\rangle", "⌊": r"\lfloor", "⌋": r"\rfloor", "⌈": r"\lceil", "⌉": r"\rceil",
    "‖": r"\|",
    # misc
    "′": "'", "″": "''", "…": r"\ldots", "⋯": r"\cdots", "⋮": r"\vdots", "⋱": r"\ddots",
    "∠": r"\angle", "△": r"\triangle", "◦": r"\circ", "°": r"^{\circ}",
}
UNI2TEX = {**GREEK, **SYMBOLS}
MATH_CHARS = set(UNI2TEX) - {"−", "·", "′", "…", "†", "‡", "∗"}
# Symbols that get wrapped in $...$ when they show up inside ordinary prose
INLINE_TEXT_MATH = set(GREEK) | set("≤≥≠≈∼≃≡∝±∓×∞∑∏∫∂∇∈∉⊂⊆∪∩→⇒⇔↦⟨⟩ℏℓ√⊗⊕∀∃≲≳∆")

# Spacing accent glyph (as TeX fonts emit it) -> combining mark -> LaTeX command
ACCENT_GLYPHS = {
    "¯": "̄", "ˉ": "̄", "˜": "̃", "~": "̃", "ˆ": "̂",
    "˙": "̇", "¨": "̈", "´": "́", "`": "̀", "ˇ": "̌",
    "˘": "̆", "⃗": "⃗",
}
COMBINING_TEX = {
    "̄": r"\bar", "̅": r"\bar", "̃": r"\tilde", "̂": r"\hat",
    "̇": r"\dot", "̈": r"\ddot", "́": r"\acute", "̀": r"\grave",
    "̌": r"\check", "̆": r"\breve", "⃗": r"\vec",
}
# A base char followed by >= 1 combining marks
# INLINE_TEXT_MATH run, optionally with accents riding on its chars ("ν̄")
# Only bar/overline/vector accents make a Latin letter math; "é", "č", "ö" in names
# are ordinary text accents and must survive as text.

# TeX's math-extension font (CMEX) often ships without a ToUnicode map, so MuPDF
# returns raw glyph codes: "X" is the display-size sum, "\x00" a big "(", "b" a wide
# hat. Pieces of assembled delimiters (extenders, bottoms) map to "".
_CMEX = (
    "()[]⌊⌋⌈⌉{}⟨⟩|‖/\\"          # 0x00-0x0F  big
    "()()[]⌊⌋⌈⌉{}⟨⟩/\\"          # 0x10-0x1F  Big / bigg
    "()[]⌊⌋⌈⌉{}⟨⟩/\\/\\"          # 0x20-0x2F  Bigg
)
CMEX_RAW = {chr(k): ch for k, ch in enumerate(_CMEX)}
CMEX_RAW.update({
    "0": "(", "1": ")", "2": "[", "3": "]", "8": "{", "9": "}", "D": "⟨", "E": "⟩",
    "F": "⊔", "G": "⊔", "H": "∮", "I": "∮", "J": "⊙", "K": "⊙", "L": "⊕", "M": "⊕",
    "N": "⊗", "O": "⊗", "P": "∑", "Q": "∏", "R": "∫", "S": "⋃", "T": "⋂", "U": "⊎",
    "V": "⋀", "W": "⋁", "X": "∑", "Y": "∏", "Z": "∫", "[": "⋃", "\\": "⋂", "]": "⊎",
    "^": "⋀", "_": "⋁", "`": "∐", "a": "∐", "b": "ˆ", "c": "ˆ", "d": "ˆ", "e": "˜",
    "f": "˜", "g": "˜", "h": "[", "i": "]", "j": "⌊", "k": "⌋", "l": "⌈", "m": "⌉",
    "n": "{", "o": "}", "p": "√", "q": "√", "r": "√", "s": "√", "t": "√", "x": "↑",
    "y": "↓", "~": "⇑", "\x7f": "⇓",
})
for _c in "4567:;<=>?@ABCuvwz{|}":
    CMEX_RAW[_c] = ""  # delimiter extenders / bottoms / brace tips: drawn, not read
del CMEX_RAW[" "]  # 0x20 is a Bigg "(", but MuPDF also synthesises spaces: leave them


def cmex_looks_raw(chars: list[dict]) -> bool:
    """Control chars or letters: a properly mapped CMEX never yields those."""
    return any(ord(c["c"]) < 32 or c["c"].isascii() and c["c"].isalpha() for c in chars)


def decode_cmex(chars: list[dict], force: bool = False) -> bool:
    """
    Map raw CMEX codes in place. Without `force`, only when the span itself is
    evidently unmapped; with `force` (the font was seen raw elsewhere in the
    document), always -- that catches spans holding nothing but "!" (a Bigg ")")
    or a real, non-synthetic space glyph (a Bigg "(").
    """
    # CMEX has no space glyph: a *real* (non-synthetic) space is code 0x20, a Bigg "("
    for c in chars:
        if c["c"] == " " and c.get("synthetic") is False and not c.get("cmex_done"):
            c["c"] = "("
            c["cmex_done"] = True  # never decode a glyph twice ("(" is also a raw code)
    if not force and not cmex_looks_raw(chars):
        return False
    for c in chars:
        if not c.get("cmex_done") and c["c"] in CMEX_RAW:
            c["c"] = CMEX_RAW[c["c"]]
            c["cmex_done"] = True
    return True


# Private-use sentinels bracketing a square root's radicand (set by
# textblocks.mark_overlines from the vinculum rule): \sqrt{...} in math, (...) in text.
RAD_OPEN, RAD_CLOSE = "", ""
# Ready-made LaTeX (an inline \frac built from a drawn fraction bar) travels inside
# one glyph between these sentinels and is copied through verbatim.
RAW_OPEN, RAW_CLOSE = "", ""

# Characters that are "math glue" when sitting next to a math token: digits, operators...
GLUE_CHARS = set("0123456789=+-−()[]{}|/<>.,:;!'′*^_ ") | {RAD_OPEN, RAD_CLOSE}
FUNC_NAMES = {"sin", "cos", "tan", "cot", "sec", "csc", "sinh", "cosh", "tanh", "log", "ln",
              "exp", "det", "dim", "ker", "deg", "gcd", "lim", "max", "min", "sup", "inf",
              "arg", "Pr", "tr", "diag"}


def word_tex(text: str, func: bool = True) -> str:
    """Upright text inside math: 'd' -> \\mathrm{d}, 'CL' -> \\text{CL}, 'max' -> \\max
    (operator names only when `func`: a subscript "exp" means "expected")."""
    s = text.strip()
    if func and s in FUNC_NAMES:
        return "\\" + s
    return rf"\mathrm{{{s}}}" if len(s) == 1 else rf"\text{{{s}}}"


def font_wrapper(font: str) -> str | None:
    """Math alphabets that live in their own fonts (blackboard, fraktur, calligraphic)."""
    f = base_font(font).lower()
    if "msbm" in f or "bbold" in f or "dsrom" in f:
        return r"\mathbb"
    if "eufm" in f or "frak" in f:
        return r"\mathfrak"
    if "rsfs" in f:
        return r"\mathscr"
    if re.search(r"cmsy|cmbsy|lmsy|txsy|maths_symbols", f):
        return r"\mathcal"
    return None


def _char_tex(ch: str, wrap: str | None) -> str:
    if ch == RAD_OPEN:
        return "{"
    if ch == RAD_CLOSE:
        return "}"
    if ch in UNI2TEX:
        cmd = UNI2TEX[ch]
        # a command ending in a letter needs a space so "\alpha x" != "\alphax"
        return cmd + " " if cmd[-1].isalpha() and cmd.startswith("\\") else cmd
    if wrap and ch.isascii() and ch.isalpha() and (wrap != r"\mathcal" or ch.isupper()):
        return f"{wrap}{{{ch}}}"
    if ch in "{}#%&$":
        return "\\" + ch
    if ch == "\\":
        return r"\backslash "
    return ch


def to_latex(text: str, font: str = "") -> str:
    """Map one span's characters to LaTeX (Greek, operators, alphabets, accents)."""
    if RAW_OPEN in text:  # pre-built LaTeX segments pass through untouched
        parts = re.split("(.*?)", text, flags=re.S)
        return "".join(p[1:-1] if p.startswith(RAW_OPEN) else to_latex(p, font) for p in parts if p)
    wrap = font_wrapper(font)
    text = unicodedata.normalize("NFD", text) if any(ord(c) > 0x7F for c in text) else text
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        marks = ""
        while i + 1 < len(text) and text[i + 1] in COMBINING_TEX:
            marks += text[i + 1]
            i += 1
        tex = _char_tex(ch, wrap).strip() if marks else _char_tex(ch, wrap)
        for m in marks:  # innermost accent first: \bar{\hat{x}}
            tex = f"{COMBINING_TEX[m]}{{{tex}}}"
        out.append(tex)
        i += 1
    return "".join(out)


def tex_join(pieces) -> str:
    """Concatenate LaTeX fragments, inserting a space only where a command needs it."""
    out = ""
    for p in pieces:
        if not p:
            continue
        if re.search(r"\\[A-Za-z]+$", out) and re.match(r"[A-Za-z0-9]", p):
            out += " "
        out += p
    out = re.sub(r"\s+", " ", out).strip()
    out = re.sub(r"\s*\\,\s*", r"\\,", out)          # "365 \,\text{fb}" -> "365\,\text{fb}"
    out = re.sub(r"(?<=[^\s\\]) ([_^])", r"\1", out)  # "M _{X}" -> "M_{X}"
    return re.sub(r"(\\[A-Za-z]+) ([_^{])", r"\1\2", out)  # "\lambda _{x}" -> "\lambda_{x}"
