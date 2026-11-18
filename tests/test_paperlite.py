"""
Regression tests for agent/paperlite: snippets that are hard to convert,
checked by hand against the PDF, so a change to the converter that breaks
them shows up here. No API call; the agent's view of the same papers is
tested in tests/test_paper_agent_view.py.

Three Belle II papers:

    2401.02840v1  R(D*) with hadronic B tagging, revtex PRD (PRD 110, 072020): the
                  published analysis of this repository's own mode (docs/references.md)
    2602.09800v2  B+ -> mu+ nu, revtex PRD (two columns, roman-numbered sections, 6 tables)
    2511.10980v2  B -> X_s nu nubar, revtex PRL letter (424 authors, supplemental material)

The PDFs are not in the repository. Fetch them into data/papers/ first; a
paper whose PDF is missing (or every paper, without pymupdf) is skipped:

    scripts/fetch_test_papers.sh
    python tests/test_paperlite.py -v

Each paper is converted once into a temporary folder (about 20 s for all
three); then:

    structure  title, figure/table/equation counts, headings, one author summary
    snippets   stacked scripts (p_{mu}^{*}), vectors and sums in running text, units,
               asymmetric errors, table rows with empty cells, numbered equations kept
               as text or linked as an image, captions, references
    hygiene    no $..$ or \\commands left, no empty scripts, no U+FFFD / private-use
               glyphs, every image link resolves, every crop is a real image in its page
    speed      each paper converts in under 60 s
    faults     known faults, written as expectedFailure tests: they pass while the
               fault is there and report "unexpected success" once it is fixed

To add a snippet: find a passage in the PDF that is easy to get wrong, check
the converter output against the page by eye, and add the exact string to
the paper's "contains" list.
"""
import json
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import pymupdf
    from agent.paperlite import convert
except ImportError:                                   # optional dependency
    pymupdf = convert = None

PAPER_DIR = ROOT / "data" / "papers"
MAX_SECONDS = 60

PAPERS = {
    "2602.09800v2": {
        "title": "Study of B^{+} → µ^{+}ν_{µ} decays at Belle and Belle II",
        "figures": 20, "tables": 6,
        # single-line equations (31), (32), (40), (48) stay text; the rest are linked crops
        "equations": ([[str(n)] for n in range(1, 7)] + [["7", "8"], ["9"], ["10", "11"], ["12", "13"]]
                      + [[str(n)] for n in range(14, 31)] + [[str(n)] for n in range(33, 40)]
                      + [["41"], ["42"], ["43", "44"], ["45"], ["46", "47"], ["49"]]),
        "headings": ["I. INTRODUCTION", "III. EVENT SELECTION", "C. Background Suppression",
                     "IV. VALIDATION USING B^{+} → D̄^{0}π^{+} DECAYS", "X. SUMMARY AND CONCLUSION",
                     "Appendix C: Fitted Distributions"],
        "authors": (350, 450),
        "contains": [
            "(The Belle and Belle II Collaborations)",
            "2.1 GeV < p_{µ}^{*} < 3.4 GeV",
            "|t_{cluster}| < 200 ns",
            "|t_{cluster}/σ_{t_{cluster}}| < 2.0",
            "| B → D^{*}ℓ^{+}ν_{ℓ} | (4.90 ± 0.12) × 10^{−2} | (5.53 ± 0.22) × 10^{−2} |",
            "| B^{+} → D̄^{0}π^{+} | | (4.66 ± 0.14) × 10^{−3} |",
            "| ∆p_{π}^{B}[GeV] MC | 0.0119 ± 0.0004 | 0.1108 ± 0.0003 |",
            "B(B^{+} → µ^{+}ν_{µ})_{Belle} = (4.7 ± 2.1 ± 1.1) × 10^{−7}, (31)",
            "∆B(B → X_{u}ℓ^{+}ν_{ℓ}) = (2.72 ± 0.05 ± 0.29) × 10^{−4}, (40)",
            "![Equation (5)](equations/eq_5_p6.png)",
            "**Table III:** Selection efficiencies of B^{+} → D̄^{0}π^{+}",
            "[1] S. Navas et al. (Particle Data Group), Phys. Rev. D 110, 030001 (2024).",
        ],
    },
    "2511.10980v2": {
        "title": "First search for B → X_{s}νν̄ decays",
        "figures": 4, "tables": 3, "equations": [],
        "headings": ["I. LIST OF BDT INPUT VARIABLES", "A. Variables related to X_{s} and B_{tag} candidates"],
        "authors": (400, 450),
        "contains": [
            "(The Belle II Collaboration)",
            "p⃗_{miss} = p⃗_{e^{+}e^{−}} − ∑ p⃗_{track} − ∑ p⃗_{γ}",
            "dr < 0.5 cm in the transverse plane and |dz| < 2 cm",
            "17° < θ < 150°",
            "M_{bc} ≡ √((√s/2)^{2} − |p_{B}^{*}c|^{2})/c^{2} > 5.27 GeV/c^{2}",
            "|E_{B}^{*} − (√s/2)| < 0.2 GeV",
            "|p⃗_{X_{s}}^{*}|",
            "T̂^{*}_{ROE(B_{tag})}",
            "θ of ∑ p⃗ of photon candidates",
            "[8.8_{−8.2}^{+8.5}(stat)_{−10.7}^{+12.6}(syst)] × 10^{−5}",
            "| [0,0.6] | 2.93 | 6_{−17−16}^{+18+19} | 0.3 ± 0.8_{−0.7}^{+0.8} | 2.2 | 2.0 |",
            "[7] I. Adachi et al. (Belle II Collaboration), Phys. Rev. D 109, 112006 (2024).",
            "B^{+} → K^{+}νν̄",
            "K^{*}(892)",
        ],
    },
    "2401.02840v1": {
        "title": ("A test of lepton flavor universality with a measurement of R(D^{*}) "
                  "using hadronic B tagging at the Belle II experiment"),
        "figures": 7, "tables": 8,
        # Eq. (4) (p6) is not detected: no crop, and its text comes out scrambled
        "equations": [["1"], ["2"], ["3"], ["5"], ["6"], ["7"], ["8"], ["9", "10"], ["11"], ["12"],
                      ["13", "14"], ["15"]],
        "headings": ["IV. RECONSTRUCTION AND EVENT SELECTION", "C. Reconstruction of the signal B meson",
                     "E. M_{miss}^{2} resolution", "VI. SAMPLE COMPOSITION AND FIT",
                     "VIII. SYSTEMATIC UNCERTAINTIES", "IX. DISCUSSION AND CONCLUSION"],
        "authors": (380, 460),
        "contains": [
            "(The Belle II Collaboration)",
            "R(D^{*}) = B(B̄ → D^{*}τ^{−}ν̄_{τ})/B(B̄ → D^{*}ℓ^{−}ν̄_{ℓ})",
            "R(D^{*}) = 0.262_{−0.039}^{+0.041}(stat)_{−0.032}^{+0.035}(syst)",
            "M_{bc} > 5.27 GeV/c^{2} and −0.15 < ∆E < 0.1 GeV",
            "(17° < θ < 150° in the laboratory frame)",
            "P_{K} = L_{K}/(L_{K} + L_{π}) > 0.1",
            "K_{S}^{0}π^{+}π^{−}π^{0}",
            "1.5 GeV^{2}/c^{4} < M_{miss}^{2} < 6.0 GeV^{2}/c^{4}",
            "![Equation (12)](equations/eq_12_p10.png)",
            "**Table VIII.** Summary of systematic uncertainties on R(D^{*}).",
            "| B̄ → D^{**}ℓ^{−}ν̄_{ℓ} branching fractions | _{−3.5%}^{+4.8%} |",
            "| PDF shapes | _{−8.3%}^{+9.1%} |",                     # stacked, no base glyph
            "| Total systematic uncertainty | _{−12.3%}^{+13.5%} |",
            "[12] M. Huschle et al. (Belle Collaboration), Phys. Rev. D 92, 072014 (2015), arXiv:1507.03233 [hep-ex].",
        ],
    },
}


def _is_pdf(path: Path) -> bool:
    """True for a real PDF; False when missing, or e.g. an HTML error page."""
    try:
        with path.open("rb") as f:
            return f.read(4) == b"%PDF"
    except OSError:
        return False


def setUpModule():
    global OUT
    OUT = Path(tempfile.mkdtemp(prefix="paperlite_test_"))


def tearDownModule():
    shutil.rmtree(OUT, ignore_errors=True)


class PaperConversion:
    """Mixin: one subclass per paper (STEM set below)."""
    STEM = ""

    @classmethod
    def setUpClass(cls):
        cls.spec = PAPERS[cls.STEM]
        if pymupdf is None:
            raise unittest.SkipTest("pymupdf not installed")
        pdf = PAPER_DIR / f"{cls.STEM}.pdf"
        if not _is_pdf(pdf):
            raise unittest.SkipTest(f"{pdf.name} missing or not a PDF in data/papers/ "
                                    "(scripts/fetch_test_papers.sh)")
        t0 = time.time()
        cls.summary = convert(str(pdf), str(OUT))
        cls.seconds = time.time() - t0
        cls.folder = OUT / cls.STEM
        cls.md = (cls.folder / f"{cls.STEM}.md").read_text(encoding="utf-8")
        cls.index = json.loads((cls.folder / "index.json").read_text(encoding="utf-8"))
        cls.pdf = pdf

    # -- structure ---------------------------------------------------------------------------
    def test_title(self):
        self.assertEqual(self.index["title"], self.spec["title"])
        self.assertTrue(self.md.startswith(f"# {self.spec['title']}\n"))

    def test_counts(self):
        self.assertEqual(len(self.index["figures"]), self.spec["figures"], "figures")
        self.assertEqual(len(self.index["tables"]), self.spec["tables"], "tables")
        self.assertEqual([e["numbers"] for e in self.index["equations"]], self.spec["equations"], "equations")

    def test_headings(self):
        found = [h["text"] for h in self.index["headings"]]
        for h in self.spec["headings"]:
            with self.subTest(heading=h):
                self.assertIn(h, found)

    def test_author_summary(self):
        lines = re.findall(r"\*Author list omitted \((\d+) names", self.md)
        self.assertEqual(len(lines), 1, "exactly one author summary (acknowledgments must not collapse)")
        lo, hi = self.spec["authors"]
        self.assertTrue(lo <= int(lines[0]) <= hi, f"{lines[0]} names")

    # -- snippets ----------------------------------------------------------------------------
    def test_snippets(self):
        for s in self.spec["contains"]:
            with self.subTest(expected=s):
                self.assertIn(s, self.md)
        for s in self.spec.get("absent", []):
            with self.subTest(absent=s):
                self.assertNotIn(s, self.md)

    # -- hygiene -----------------------------------------------------------------------------
    def test_no_tex_left(self):
        self.assertNotRegex(self.md, r"(?<!\\)\$[^$\n]*\$", "inline $..$ math")
        self.assertNotRegex(self.md, r"\\[A-Za-z]{2,}", "LaTeX command")

    def test_no_broken_glyphs(self):
        self.assertNotIn("\ufffd", self.md)
        self.assertNotRegex(self.md, r"[\^_]\{\s*\}", "empty script")
        bad = [c for c in self.md if 0xE000 <= ord(c) <= 0xF8FF or c == "\u2063"]
        self.assertEqual(bad, [], "private-use / placeholder characters")

    def test_links_and_crops(self):
        page_rects = [p.rect for p in pymupdf.open(self.pdf)]
        links = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", self.md)
        self.assertTrue(links or not (self.spec["figures"] or self.spec["equations"]))
        for link in links:
            with self.subTest(link=link):
                f = self.folder / link
                self.assertTrue(f.exists(), "linked file missing")
                pix = pymupdf.Pixmap(str(f))
                self.assertGreater(pix.width * pix.height, 40 * 20, "crop too small")
        for rec in self.index["figures"] + self.index["tables"] + self.index["equations"]:
            if rec.get("bbox"):
                with self.subTest(crop=rec.get("file")):
                    r = pymupdf.Rect(rec["bbox"])
                    self.assertTrue(page_rects[rec["page"] - 1].contains(r), "crop outside its page")
                    self.assertFalse(r.is_empty)

    # -- speed -------------------------------------------------------------------------------
    def test_speed(self):
        self.assertLess(self.seconds, MAX_SECONDS)


class TestRevtexPRD(PaperConversion, unittest.TestCase):
    STEM = "2602.09800v2"

    @unittest.expectedFailure
    def test_known_fault_heading_above_figure(self):
        # p22: "Appendix B: Continuum Correction using Off-Resonance Data" sits
        # directly above the full-width Fig. 18 and is lost with the figure
        # region; its figure ends up under Appendix A.
        found = [h["text"] for h in self.index["headings"]]
        self.assertIn("Appendix B: Continuum Correction using Off-Resonance Data", found)

    @unittest.expectedFailure
    def test_known_fault_label_over_arrow(self):
        # Table I: "B+ ->(WA) X_u l+ nu" has "WA" set over the arrow; the
        # converter splits it into "B^{+W} → ^{A}X_{u}...".
        row = next(line for line in self.md.splitlines() if "(1.99 ± 13.9)" in line)
        self.assertIn("WA", row)


class TestRevtexPRL(PaperConversion, unittest.TestCase):
    STEM = "2511.10980v2"


class TestRDstar(PaperConversion, unittest.TestCase):
    STEM = "2401.02840v1"

    @unittest.expectedFailure
    def test_known_fault_heading_starting_with_math(self):
        # p7: "B. B̄ → D**ℓ−ν̄ℓ composition" starts with math and is not taken
        # as a heading; its text lands under "A. Efficiency correction", so the
        # agent cannot ask for the D** validation by name.
        found = [h["text"] for h in self.index["headings"]]
        self.assertTrue(any(re.match(r"B\. .*composition$", h) for h in found), found)

    def test_stacked_errors_in_table(self):
        # Table VIII (systematics): each cell is a stacked "+a% / −b%" pair with
        # no base glyph. Sorting the glyphs by x alone used to interleave the
        # two lines ("− + 98..13%%"); every row must read _{−b%}^{+a%}.
        start = self.md.index("| Source | Uncertainty |")
        rows = self.md[start:].split("\n\n")[0].splitlines()[2:]
        self.assertEqual(len(rows), 12)
        for row in rows:
            with self.subTest(row=row):
                self.assertRegex(row, r"\| _\{−\d+\.\d%\}\^\{\+\d+\.\d%\} \|$")

    @unittest.expectedFailure
    def test_known_fault_author_list_continues_on_page_2(self):
        # The author list runs onto page 2; only page 1 is summarized, and the
        # last names (", X. Y. Zhou , V. I. Zhukova , ...") stay in the text.
        self.assertNotIn("Zhukova", self.md)


if __name__ == "__main__":
    unittest.main()
