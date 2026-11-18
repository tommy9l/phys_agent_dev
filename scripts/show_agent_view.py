#!/usr/bin/env python3
"""
Show exactly what the agent receives from read_paper / view_figure for a
paper in data/papers/, without any API call. Useful to judge a paper before
an agent run, and to see what is lost (equations, figure-only values).

    python scripts/show_agent_view.py                       # size table, all papers
    python scripts/show_agent_view.py 2602.09800v2          # the first view
    python scripts/show_agent_view.py 2602.09800v2 --next   # + the suggested method read
    python scripts/show_agent_view.py 2602.09800v2 --section "VI. SYST" --pages 6
    python scripts/show_agent_view.py 2602.09800v2 --figure "Table III" --figure "Eq. (5)"
    python scripts/show_agent_view.py 2602.09800v2 --next --save

--save writes every result to data/papers/<stem>/agent_view/ (gitignored):
first_view.md, next.md, section.md, pages.md, and for each figure the text
part (.txt) plus the image the agent would see (.png).

Sizes are given in characters and ~tokens at 2.5 characters per token (the
API token counter gave 2.2-2.7 on the Markdown of these Belle II papers;
math and Unicode symbols cost more tokens than plain English).
"""
import argparse
import base64
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import tools  # noqa: E402


CHARS_PER_TOKEN = 2.5


def tok(text: str) -> int:
    return round(len(text) / CHARS_PER_TOKEN)


def size(text: str) -> str:
    return f"{len(text):,} chars, ~{round(len(text) / CHARS_PER_TOKEN):,} tokens"


def next_sections(view: str) -> str:
    m = re.search(r"^Next \([^)]*\): read_paper\(name, section=(['\"])(.*)\1\)$",
                  view, re.M)
    return m.group(2) if m else ""


def table() -> None:
    pdfs = sorted(tools.PAPER_DIR.glob("*.pdf"))
    if not pdfs:
        sys.exit("no PDFs in data/papers/ (scripts/fetch_test_papers.sh)")
    print(f"{'paper':16s} {'pages':>5s} {'first view':>12s} {'Next read':>11s} "
          f"{'full Markdown':>14s}   (~tokens at 2.5 chars/token)")
    for pdf in pdfs:
        view = tools.read_paper.call({"name": pdf.stem})
        if view.startswith("error"):
            print(f"{pdf.stem:16s} {view}")
            continue
        nxt = next_sections(view)
        nread = tools.read_paper.call({"name": pdf.stem, "section": nxt}) if nxt else ""
        md = (tools.PAPER_DIR / pdf.stem / f"{pdf.stem}.md").read_text(encoding="utf-8")
        pages = re.search(r"(\d+) pages", view)
        print(f"{pdf.stem:16s} {pages.group(1) if pages else '?':>5s} "
              f"{tok(view):>12,} {tok(nread):>11,} {tok(md):>14,}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paper", nargs="?", help="PDF stem in data/papers/; omit for the size table")
    ap.add_argument("--next", action="store_true", help="also run the suggested method read")
    ap.add_argument("--section", help='section= argument, e.g. "III. EVENT; VI. SYST"')
    ap.add_argument("--pages", help='pages= argument, e.g. "6" or "3-6"')
    ap.add_argument("--figure", action="append", default=[],
                    help='view_figure argument, e.g. "Table III" (repeatable)')
    ap.add_argument("--save", action="store_true",
                    help="write the results to data/papers/<stem>/agent_view/")
    a = ap.parse_args()
    if not a.paper:
        table()
        return

    results = {}
    view = tools.read_paper.call({"name": a.paper})
    results["first_view.md"] = view
    if not view.startswith("error") and a.next and (nxt := next_sections(view)):
        results["next.md"] = tools.read_paper.call({"name": a.paper, "section": nxt})
    if a.section:
        results["section.md"] = tools.read_paper.call({"name": a.paper, "section": a.section})
    if a.pages:
        results["pages.md"] = tools.read_paper.call({"name": a.paper, "pages": a.pages})

    images = {}
    for fig in a.figure:
        slug = re.sub(r"[^A-Za-z0-9]+", "_", fig).strip("_").lower()
        res = tools.view_figure.call({"name": a.paper, "figure": fig})
        if isinstance(res, str):
            results[f"figure_{slug}.txt"] = res
            continue
        results[f"figure_{slug}.txt"] = res[0]["text"] + "\n[+ one PNG image block]"
        images[f"figure_{slug}.png"] = base64.b64decode(res[1]["source"]["data"])

    for name, text in results.items():
        print(f"\n===== {name}  ({size(text)}) " + "=" * 20)
        print(text)

    if a.save:
        stem = a.paper.removesuffix(".pdf")      # "2602.09800v2": not Path.stem
        out = tools.PAPER_DIR / stem / "agent_view"
        out.mkdir(parents=True, exist_ok=True)
        for name, text in results.items():
            (out / name).write_text(text, encoding="utf-8")
        for name, data in images.items():
            (out / name).write_bytes(data)
        print(f"\nsaved {len(results) + len(images)} files to {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
