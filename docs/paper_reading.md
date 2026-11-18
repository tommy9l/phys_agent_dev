# Reading published papers: what the agent digests

The analysis policy asks the agent to follow the variables and selection of a
published analysis. With `read_paper` and `view_figure` it reads the paper
itself instead of only the curated summaries in `docs/references.md`. A PDF
the student puts in `data/papers/` is converted once by `agent/paperlite`
(PyMuPDF, no API call; again only when the PDF or the converter changes) into Markdown, an `index.json` and figure, table and
equation crops. The agent then reads that conversion in small pieces.

This page shows what the agent receives, call by call, on three Belle II
papers, and where the approach works and where it does not. Everything here
can be reproduced without an API call:

```bash
scripts/fetch_test_papers.sh                                  # the three PDFs -> data/papers/
python scripts/show_agent_view.py                             # size table
python scripts/show_agent_view.py 2602.09800v2 --next --figure "Eq. (5)" --save
python tests/test_paper_agent_view.py -v                      # what the agent sees, pinned
python tests/test_paperlite.py -v                             # converter regression checks
```

## One paper, call by call

The example is the B⁺ → µ⁺ν paper (arXiv:2602.09800, 25 pages, revtex).

**1. `read_paper()`**: which papers are available.

```
Papers in data/papers/:
- 2602.09800v2.pdf: Study of B^{+} → µ^{+}ν_{µ} decays at Belle and Belle II
- ...
```

**2. `read_paper("2602.09800v2")`**: the short first view (15k chars,
~6k tokens). It holds the outline, where the selection is stated, the figure
and table list, and the abstract and conclusion. Excerpt:

```
Sections ([shown] / [partly shown] / [method, ask by name] / ...):
- (front matter: abstract) (p1, 1,985 chars) [shown]
- I. INTRODUCTION (p2, 5,582 chars) [ask by name]
- II. DETECTOR, SIMULATED SAMPLES AND CORRECTIONS (p3, 50 chars) [details, ask by name]
- III. EVENT SELECTION (p6, 23 chars) [method, ask by name]
  - A. Signal and Tag-side Reconstruction (p6, 3,923 chars) [method, ask by name]
  ...
- X. SUMMARY AND CONCLUSION (p18, 4,059 chars) [shown]

Where the selection is stated (pattern-matched; verify values in the paper):
- variables cut on: p_{µ}^{B}, m_{N}, E_{γ}^{B}, p_{µ}^{*}, θ, P_{µ}, |dz|, p_{T}, |t_{cluster}|, ...
- requirements: 2.1 GeV < p_{µ}^{*} < 3.4 GeV (p6); 17° < θ < 150° (p6); P_{µ} > 0.99 (p6);
  |dz| < 2 cm (p6); p_{T} > 100 MeV (p6); |t_{cluster}| < 200 ns (p6); θ_{thrust} < 0.8 (p9); ...
- selection/variable tables: Table III (p10); Table IV (p11); Table VI (p24)

Next (selection, signal, backgrounds, fit, systematics): read_paper(name, section='III. EVENT
SELECTION; V. STATISTICAL ANALYSIS AND LIMIT SETTING PROCEDURE; VI. SYSTEMATIC UNCERTAINTIES')
Details (samples, simulation, method, inputs, training): read_paper(name, section='II. DETECTOR,
SIMULATED SAMPLES AND CORRECTIONS; IV. VALIDATION USING B^{+} → D̄^{0}π^{+} DECAYS')
```

**3. The suggested `section=` call**: the method sections the plan needs
(23.7k chars, ~9.5k tokens), with their subsections. Introduction, detector
description, acknowledgements, references and appendices are never sent
unless asked for by name.

**4. `view_figure("2602.09800v2", "Eq. (5)")`**: only when the text is not
enough. The extracted text of this display equation is out of order:

```
Equation (5): ∑ /  / (5) / p_{tag}^{lab} = / p^{lab} +_{i} ∑ _{j} p^{lab}, / j ∈ γ / i ∈charged
```

The image block returned with it (a 692 × 77 px crop) shows the agent the
actual equation, p⃗_tag^lab = Σ_{i∈charged} p⃗_i^lab + Σ_{j∈γ} p⃗_j^lab.
Tables work the same way: `view_figure(..., "Table III")` returns the crop of
the efficiency table.

## Context cost per paper

| Paper | Pages | PDF sent to the model as a document | Full paperlite Markdown | First view | Suggested method read |
|---|---:|---:|---:|---:|---:|
| 2511.10980 B → X_sνν̄ (PRL letter) | 12 | 43.7k | 22.8k | ~6.2k | — (letter) |
| 2401.02840 R(D\*), hadronic tag (revtex) | 16 | not counted | ~27.7k | ~3.6k | ~13.7k |
| 2602.09800 B⁺ → µ⁺ν (revtex) | 25 | 84.9k | 38.4k | ~6.1k | ~9.5k |

Tokens. The PDF and full-Markdown columns are exact counts from the API's
token counter, except for R(D\*), which was added later and is estimated. The
estimates use 2.5 characters per token: the counts give 2.2–2.7 for this
Markdown, because math and Unicode symbols cost more tokens than plain
English. A letter has no section headings, so its first view shows the start
of the main text and the locator gives the pages of the rest (`pages="3-5"`).
Four more papers were checked while developing (a JHEP paper, an Elsevier
paper with an ML method, a 34-page letter with appendices, an instrumentation
paper; up to 112.5k tokens as a PDF); what they showed is pinned on synthetic
text in `tests/test_offline.py`, and any of them can be fetched by id, e.g.
`scripts/fetch_test_papers.sh 2602.19807v2`.

## Converter regression test

`tests/test_paperlite.py` converts the three papers (about 20 s, no API
call) and checks strings that are easy to get wrong, each compared by eye
with the PDF page: stacked and nested scripts
(`2.1 GeV < p_{µ}^{*} < 3.4 GeV`, `|t_{cluster}/σ_{t_{cluster}}| < 2.0`),
vectors and sums in running text, asymmetric errors, table rows with empty
cells (`| B^{+} → D̄^{0}π^{+} | | (4.66 ± 0.14) × 10^{−3} |`), numbered
equations kept as text or linked as a crop, captions and references. It
also checks the structure (title, figure/table/equation counts, headings)
and hygiene (no leftover LaTeX, no broken glyphs, every crop inside its
page). A converter change that breaks one of these fails the test. To add a
case, check a passage against the PDF and add the exact string to the
paper's `contains` list.

## Strengths and faults

Each line is pinned by a test in `tests/test_paper_agent_view.py`,
`tests/test_paperlite.py` or `tests/test_offline.py`. Faults are
`expectedFailure` tests: they report "unexpected success" once fixed.

| | What | Example | Test |
|---|---|---|---|
| ✓ | Physics notation survives as text | `[8.8_{−8.2}^{+8.5}(stat)_{−10.7}^{+12.6}(syst)] × 10^{−5}` | `test_snippets` (`test_paperlite.py`) |
| ✓ | Stacked uncertainties in tables | R(D\*) Table VIII, a cell with no central value: `\| PDF shapes \| _{−8.3%}^{+9.1%} \|` | `test_stacked_errors_in_table` |
| ✓ | The locator finds the selection with pages | `\|E_{B}^{*} − (√s/2)\| < 0.2 GeV (p3)`, `120 < m_{γγ} < 145 MeV/c^{2} (p3)` | `test_locator_finds_selection` |
| ✓ | Results first, method on request | B → µν: abstract and conclusion shown, selection named in `Next` | `test_abstract_and_conclusion_first`, `test_next_call_reads_the_method` |
| ✓ | The analysis this repository follows | R(D\*): a ~3.6k-token first view with the result, the cuts on R₂, ΔE, M_bc, M²_miss (with its GeV²/c⁴ units) and E_ECL located, and `Next` naming the selection, fit and systematics sections | `test_results_first_method_on_request`, `test_locator_finds_selection` (`TestRDstar`) |
| ✓ | Letters without headings | PRL main text shown; unheaded reference list and supplemental material held back | `test_letter_main_text_is_shown`, `test_supplemental_material_is_back_matter` |
| ✓ | JHEP title page | the abstract under "The Belle II Collaboration" is shown, the contents page skipped | `test_paper_title_page_and_locator_patterns` (offline) |
| ✓ | ML and instrumentation papers | DNN inputs/training and PID performance offered under `Details` | `test_paper_title_page_and_locator_patterns` (offline) |
| ✓ | Clean crops | Table III of the B → µν paper; Table VIII (systematics) of R(D\*) | `test_view_figure_returns_the_table_crop`, `test_view_figure_returns_the_systematics_table`, `test_links_and_crops` |
| ✗ | Numbered display equations are images | Eq. (5) text out of order; the agent needs `view_figure` (~0.1k tokens for an equation crop) | `test_known_fault_numbered_equation_as_text` |
| ✗ | A heading directly above a full-width figure is lost | "Appendix B: Continuum Correction using Off-Resonance Data" (B → µν, p22); its figure ends up under Appendix A | `test_known_fault_heading_above_figure` |
| ✗ | A label set over an arrow is split | Table I: B⁺ →(WA) X_uℓ⁺ν comes out as `B^{+W} → ^{A}X_{u}ℓ^{+}ν_{ℓ}` | `test_known_fault_label_over_arrow` |
| ✗ | A heading that starts with math is lost | R(D\*) p7: "B. B̄ → D\*\*ℓ⁻ν̄ composition"; its text (the D\*\* validation) lands under "A. Efficiency correction" | `test_known_fault_heading_starting_with_math` |
| ✗ | An author list running onto page 2 | only page 1 is summarized; the last names stay in the first view | `test_known_fault_author_list_continues_on_page_2` |
| ✗ | The locator is a pattern match | misses `M_bc > 5.27 GeV/c²` at the end of its definition and `E_vis` "greater than 4 GeV" stated in words; also lists control-region cuts and misses cuts only in tables | `test_known_fault_cut_inside_a_definition`, `test_known_fault_cut_stated_in_words` |
| ✗ | Long letters | only the first ~15k chars of the main text in the first view | `test_letter_main_text_is_shown` |
| ✗ | Not covered | scanned PDFs (no text layer); values that exist only inside a figure (e.g. a number in a legend; `view_figure` shows them); two-line formulas in running text can scramble word order (paperlite README) | — |

## What is not measured yet

The checks above measure what reaches the agent, not whether it plans a better
analysis with it. That needs an end-to-end run archived in `docs/tasks/`, for
example a plan for a B⁺ → µ⁺ν selection following arXiv:2602.09800, with the
planned cuts compared against the paper and the run's `[usage]` line.
