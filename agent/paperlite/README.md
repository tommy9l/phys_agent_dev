# paperlite

Turns scientific-paper PDFs into Markdown an LLM can read. It is one package and needs only
Python 3.11 and `pymupdf>=1.24`. Tested on Belle II revtex, JHEP and Elsevier papers and a
126-page white paper; the 7 test papers take about 24 s with `-j 4`.

In this repository it backs the agent's `read_paper` tool (`agent/tools.py`). Put the PDFs in
**`data/papers/`** (gitignored); that folder is both the default input and the default output.
The agent converts a paper on its first `read_paper` call, so running the CLI by hand is optional.
Run from the repository root:

```bash
python -m agent.paperlite                                # every data/papers/*.pdf -> data/papers/<stem>/
python -m agent.paperlite data/papers/*.pdf -j 4         # same, 4 processes
python -m agent.paperlite paper.pdf --no-images          # text + index only
python -m agent.paperlite paper.pdf --keep-authors       # full author list instead of one line
python -m agent.paperlite paper.pdf -o somewhere/else    # another output root
```

```python
from agent.paperlite import convert
convert("data/papers/paper.pdf")   # -> data/papers/paper/; returns {"pages", "figures", "tables", "equations", "chars", "seconds"}
```

## Output (one folder per paper, next to the PDF)

```
data/papers/<stem>.pdf                  the input
data/papers/<stem>/<stem>.md            Markdown, with <!-- page N --> markers
data/papers/<stem>/images/              fig_<N>_p<page>.png
data/papers/<stem>/tables/              table_<N>_p<page>.png (the Markdown table is in the .md)
data/papers/<stem>/equations/           eq_<n>_p<page>.png for numbered equations
data/papers/<stem>/index.json           title, headings, figures, tables, equations: page, file, crop box, caption/text
                                        (+ "locator": where the selection is stated, added by read_paper)
```

## Notation

All math is written in one plain notation: Unicode symbols, `_{…}` / `^{…}`, units as words.

```
p⃗_{miss} = p⃗_{e^{+}e^{−}} − ∑ p⃗_{track} − ∑ p⃗_{γ}
M_{bc} ≡ √((√s/2)^{2} − |p_{B}^{*}c|^{2})/c^{2} > 5.27 GeV/c^{2}
B(B → X_{s}νν̄) = [8.8_{−8.2}^{+8.5}(stat)_{−10.7}^{+12.6}(syst)] × 10^{−5}
```

- **Numbered equations** are PNG crops linked as `![Equation (7)](equations/eq_7_p6.png)`. The number in the alt text maps "Eq. (7)" in the prose to its file.
- **Unnumbered display math** stays as text.
- **Tables** are Markdown tables, with a link to the crop.
- **Author lists** become one summary line; the collaboration line is kept.

## Modules

| module | job |
|---|---|
| `layout` | columns, gutter, line spacing (measured per paper), reading order, headings, captions, crops, author list, contents lists, references, Markdown; rewrites the engine's LaTeX into plain notation |
| `textblocks` | rawdict blocks + glyph repairs: script fragments, accents, overlines, radicands, raw TeX sums/brackets |
| `inline` | a line's glyphs → tokens → text with `$math$` |
| `texmap` | Unicode ↔ LaTeX tables, raw TeX math-extension (CMEX) glyphs |
| `tablekit` | caption-anchored tables → Markdown / CSV / LaTeX / JSON |
| `core` | regexes, geometry/font helpers |

## Tests

In this repository, `tests/test_offline.py` (`test_read_paper_*`) converts one of the analysis notes
in `docs/notes/` through the agent's `read_paper` tool and checks the outline, the section triage
and the path guards.

`tests/test_paperlite.py` is the converter's regression test. It converts three Belle II papers, which `scripts/fetch_test_papers.sh` downloads into `data/papers/`
(the test is skipped without them):

- `2401.02840v1`: revtex PRD, the hadronic-tag R(D\*) measurement in `docs/references.md`
- `2602.09800v2`: revtex PRD
- `2511.10980v2`: revtex PRL letter

It checks:

- **Structure:** title, figure/table/equation counts, headings, the author summary.
- **Snippets:** hand-checked strings that are easy to break: vectors and sums, stacked and nested scripts, units, asymmetric errors, table rows with empty cells, equations, captions, references.
- **Hygiene:** no leftover `$`, `\commands`, empty scripts or broken glyphs; every link and crop is valid.
- **Speed:** each paper converts in under 60 s.
- **Known faults:** `expectedFailure` tests that report "unexpected success" once fixed.

```bash
scripts/fetch_test_papers.sh
python tests/test_paperlite.py -v                                # ~20 s
```

## Known limitations

- **Two-line formulas in running text:** tall radicals, big operators with limits and stacked fractions can scramble the word order of those lines. One example is the m_bc definition in 2602.09800 (page 6), where "√s/2 between −2.64 GeV …" lands before the radical it belongs to. About 95% of the text is unaffected, and agents can read the passage from context or the page image.
- **Unnumbered display math:** 2-D formulas such as cases braces or nested limits read only roughly as text.
- **Scanned PDFs:** need OCR first; there is no text layer to read.
