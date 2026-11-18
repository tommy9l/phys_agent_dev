"""
Optional tests of what the agent actually receives from read_paper and
view_figure on three real Belle II papers: the hadronic-tag R(D*)
measurement this repository follows (docs/references.md), a long revtex
paper with numbered sections, and a PRL letter without section headings.
Behaviour seen only on other papers (JHEP title page, an ML method section,
appendices of a letter, a paper without cuts) is checked on synthetic text
in tests/test_offline.py.

The PDFs are not in the repository. Fetch them into data/papers/ first;
without them (or without pymupdf) every test here is skipped:

    scripts/fetch_test_papers.sh
    python tests/test_paper_agent_view.py -v

Each class pins what the agent should see for one paper: which sections the
short first view shows, which method sections its "Next" call names, which
cuts the locator finds, and what a figure crop returns. Known faults are
written as expectedFailure tests: they state the behaviour we want, pass
while the fault is there ("expected failure"), and report "unexpected
success" once it is fixed, so the decorator can be removed.

At the end the size of each view is printed (chars and ~tokens at 2.5 chars
per token, the ratio the API token counter gave on these papers' Markdown),
i.e. how much context one paper costs the agent.
"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import tools  # noqa: E402

try:
    import pymupdf  # noqa: F401
except ImportError:
    pymupdf = None

SIZES: dict[str, dict[str, int]] = {}


def _call(tool, **kw):
    return tool.call(kw)


def _is_pdf(path: Path) -> bool:
    """True for a real PDF; False when missing, or e.g. an HTML error page."""
    try:
        with path.open("rb") as f:
            return f.read(4) == b"%PDF"
    except OSError:
        return False


class PaperView:
    """Mixin: one subclass per paper (STEM set below)."""
    STEM = ""

    @classmethod
    def setUpClass(cls):
        if pymupdf is None:
            raise unittest.SkipTest("pymupdf not installed")
        if not _is_pdf(tools.PAPER_DIR / f"{cls.STEM}.pdf"):
            raise unittest.SkipTest(f"{cls.STEM}.pdf missing or not a PDF in data/papers/ "
                                    "(scripts/fetch_test_papers.sh)")
        cls.view = _call(tools.read_paper, name=cls.STEM)
        cls.outline, _, cls.body = cls.view.partition("\n---\n")
        out = tools.PAPER_DIR / cls.STEM
        cls.md = (out / f"{cls.STEM}.md").read_text(encoding="utf-8")
        cls.index = json.loads((out / "index.json").read_text(encoding="utf-8"))
        m = re.search(r"^Next \([^)]*\): read_paper\(name, section=(['\"])(.*)\1\)$",
                      cls.outline, re.M)
        cls.next_sections = m.group(2) if m else ""
        cls.next_read = (_call(tools.read_paper, name=cls.STEM,
                               section=cls.next_sections)
                         if cls.next_sections else "")
        SIZES[cls.STEM] = {"first view": len(cls.view),
                           "Next read": len(cls.next_read),
                           "full Markdown": len(cls.md)}

    def status(self, title: str) -> str:
        """The [tag] the outline gives the first section whose title starts so."""
        for line in self.outline.splitlines():
            m = re.match(r"^\s*- (.+) \(p\d+, [\d,]+ chars\) \[([^\]]+)\]$", line)
            if m and m.group(1).startswith(title):
                return m.group(2)
        self.fail(f"no outline entry starting with {title!r}")

    def assertLocated(self, cut: str):
        """The locator lists this requirement (checked in index.json too)."""
        self.assertIn(cut, self.outline)
        cuts = [c["cut"] for c in self.index["locator"]["cuts"]]
        self.assertTrue(any(cut.startswith(c) or c in cut for c in cuts), cuts)

    # common to every paper
    def test_first_view_is_short(self):
        self.assertLessEqual(len(self.view), tools.PAPER_DEFAULT_BUDGET)

    def test_references_never_shown(self):
        self.assertNotRegex(self.body, r"(?m)^\[1\] ")


class TestRevtexLetter(PaperView, unittest.TestCase):
    """2511.10980v2, B → X_s νν̄, PRL letter: no section headings."""
    STEM = "2511.10980v2"

    def test_letter_main_text_is_shown(self):
        self.assertEqual(self.status("(main text, no section headings)"),
                         "partly shown")             # 32k chars > budget
        self.assertIn("section continues", self.body)

    def test_supplemental_material_is_back_matter(self):
        self.assertEqual(self.status("References (unheaded list)"), "back matter")
        self.assertEqual(self.status("I. LIST OF BDT INPUT VARIABLES"), "back matter")

    def test_locator_finds_selection(self):
        self.assertLocated("|E_{B}^{*} − (√s/2)| < 0.2 GeV (p3)")
        self.assertLocated("17° < θ < 150° (p3)")
        self.assertLocated("120 < m_{γγ} < 145 MeV/c^{2} (p3)")

    @unittest.expectedFailure
    def test_known_fault_cut_inside_a_definition(self):
        # "M_bc ≡ √((√s/2)² − |p_B* c|²)/c² > 5.27 GeV/c²": the cut sits at
        # the end of the definition, so the pattern match does not see it.
        self.assertLocated("M_{bc} > 5.27 GeV/c^{2}")


class TestRevtexLongPaper(PaperView, unittest.TestCase):
    """2602.09800v2, B⁺ → µ⁺ν, revtex PRD with roman-numbered sections."""
    STEM = "2602.09800v2"

    def test_abstract_and_conclusion_first(self):
        # A long paper with a large figure list: abstract and conclusion
        # fit, the detailed result subsections (12k chars) do not.
        self.assertEqual(self.status("(front matter: abstract)"), "shown")
        self.assertEqual(self.status("X. SUMMARY AND CONCLUSION"), "shown")
        self.assertIn(self.status("A. Search for B^{+}"),
                      ("shown", "partly shown", "over budget"))
        self.assertEqual(self.status("III. EVENT SELECTION"), "method, ask by name")

    def test_next_call_reads_the_method(self):
        self.assertIn("III. EVENT SELECTION", self.next_sections)
        self.assertIn("VI. SYSTEMATIC UNCERTAINTIES", self.next_sections)
        self.assertIn("### C. Background Suppression", self.next_read)
        self.assertIn("## VI. SYSTEMATIC UNCERTAINTIES", self.next_read)

    def test_locator_finds_selection(self):
        self.assertLocated("2.1 GeV < p_{µ}^{*} < 3.4 GeV (p6)")
        self.assertLocated("|t_{cluster}| < 200 ns (p6)")

    def test_view_figure_returns_the_table_crop(self):
        res = _call(tools.view_figure, name=self.STEM, figure="Table III")
        self.assertEqual([b["type"] for b in res], ["text", "image"])
        self.assertIn("Selection efficiencies", res[0]["text"])

    @unittest.expectedFailure
    def test_known_fault_numbered_equation_as_text(self):
        # Display equations are kept as images; the text paperlite extracts
        # for Eq. (5), p⃗_tag^lab = Σ_{i∈charged} p⃗_i^lab + Σ_{j∈γ} p⃗_j^lab,
        # comes out of order. The agent has to look at it with view_figure.
        eq = next(e for e in self.index["equations"] if e["numbers"] == ["5"])
        self.assertRegex(eq["text"], r"p⃗?_\{tag\}\^\{lab\} = ∑_\{i")


class TestRDstar(PaperView, unittest.TestCase):
    """2401.02840v1, R(D*) with hadronic B tagging at Belle II (PRD 110,
    072020): the published analysis of this repository's own mode."""
    STEM = "2401.02840v1"

    def test_results_first_method_on_request(self):
        self.assertEqual(self.status("(front matter: abstract)"), "shown")
        self.assertEqual(self.status("VII. RESULTS"), "shown")
        self.assertEqual(self.status("IX. DISCUSSION AND CONCLUSION"), "shown")
        self.assertEqual(self.status("IV. RECONSTRUCTION AND EVENT SELECTION"),
                         "method, ask by name")
        self.assertEqual(self.status("III. DATA AND SIMULATION"), "details, ask by name")

    def test_next_call_reads_the_method(self):
        self.assertEqual(self.next_sections, "IV. RECONSTRUCTION AND EVENT SELECTION; "
                         "VI. SAMPLE COMPOSITION AND FIT; VIII. SYSTEMATIC UNCERTAINTIES")
        self.assertIn("### C. Reconstruction of the signal B meson", self.next_read)
        self.assertIn("## VIII. SYSTEMATIC UNCERTAINTIES", self.next_read)

    def test_locator_finds_selection(self):
        self.assertLocated("R_{2} < 0.4 (p4)")
        self.assertLocated("−0.15 < ∆E < 0.1 GeV (p4)")
        self.assertLocated("M_{bc} > 5.27 GeV/c^{2} (p4)")
        self.assertLocated("1.5 GeV^{2}/c^{4} < M_{miss}^{2} < 6.0 GeV^{2}/c^{4} (p10)")

    def test_view_figure_returns_the_systematics_table(self):
        # The systematics table: its text reads _{−8.3%}^{+9.1%} per row
        # (test_paperlite.py); the crop shows the same with the layout.
        res = _call(tools.view_figure, name=self.STEM, figure="Table VIII")
        self.assertEqual([b["type"] for b in res], ["text", "image"])
        self.assertIn("Summary of systematic uncertainties", res[0]["text"])

    @unittest.expectedFailure
    def test_known_fault_cut_stated_in_words(self):
        # "we require the measured visible energy, E_vis, ... to be greater
        # than 4 GeV": the locator only matches cuts written with symbols.
        self.assertLocated("E_{vis} > 4 GeV")


def tearDownModule():
    if SIZES:
        print("\ncontext per paper (chars, ~tokens at 2.5 chars/token):")
        print(f"  {'paper':14s} {'first view':>18s} {'Next read':>18s} {'full Markdown':>18s}")
        for stem, s in SIZES.items():
            cells = [f"{n:,} (~{round(n / 2.5):,})" for n in s.values()]
            print(f"  {stem:14s} " + " ".join(f"{c:>18s}" for c in cells))


if __name__ == "__main__":
    unittest.main()
