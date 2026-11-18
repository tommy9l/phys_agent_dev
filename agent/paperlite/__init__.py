"""
paperlite - scientific-paper PDFs -> Markdown an LLM can read (PyMuPDF only).

    python -m agent.paperlite [paper.pdf ...] [-o data/papers] [-j 4] [--dpi 200]
                              [--no-images] [--keep-authors]
    (no PDFs given: converts every data/papers/*.pdf)

    from agent.paperlite import convert
    convert("data/papers/paper.pdf")    # -> data/papers/paper/paper.md, images/, tables/, equations/, index.json

Modules
    layout      columns, reading order, headings, captions, crops, author list, contents, Markdown
    textblocks  rawdict blocks + glyph repairs (script fragments, accents, overlines, radicands)
    inline      a line's glyphs -> tokens -> text with $math$ (rewritten to plain notation by layout)
    texmap      Unicode <-> LaTeX tables, raw TeX math-extension (CMEX) glyphs
    tablekit    caption-anchored tables -> Markdown / CSV / LaTeX / JSON
    core        regexes, geometry/font helpers
"""
from .layout import convert, main

__all__ = ["convert", "main"]
__version__ = "1.0.0"
