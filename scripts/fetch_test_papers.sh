#!/usr/bin/env bash
# Download the Belle II papers used by the optional paper tests
# (tests/test_paperlite.py, tests/test_paper_agent_view.py)
# into data/papers/ (gitignored). Existing PDFs are kept.
#
#   scripts/fetch_test_papers.sh                  # the three test papers (53 pages)
#   scripts/fetch_test_papers.sh 2602.19807v2     # any other arXiv id(s) instead
#
# The three are the hadronic-tag R(D*) measurement this repository follows
# (docs/references.md), a long revtex paper with numbered sections and a PRL
# letter without headings; together they carry the known faults pinned as
# expectedFailure tests.
#
# This script never fails its caller. If arxiv.org cannot be reached, or a
# download fails or is not a PDF, it says which papers are missing and
# exits 0; the paper tests then skip those papers. Without network access,
# put the PDFs into data/papers/<id>.pdf by hand.
# ARXIV=<url> points it at a mirror (default https://arxiv.org).
set -uo pipefail

cd "$(dirname "$0")/.."
mkdir -p data/papers
ARXIV="${ARXIV:-https://arxiv.org}"

if (( $# )); then
  papers=("$@")
else
  papers=(
    2401.02840v1   # R(D*) with hadronic B tagging at Belle II, PRD 110, 072020 (16 pages)
    2602.09800v2   # B+ -> mu+ nu at Belle and Belle II (revtex PRD, 25 pages)
    2511.10980v2   # First search for B -> Xs nu nubar (PRL letter, 12 pages)
  )
fi

is_pdf() {            # a real PDF starts with "%PDF" (an HTML error page does not)
  [[ -s "$1" ]] && head -c 4 "$1" | grep -q '^%PDF'
}

missing=()
for id in "${papers[@]}"; do
  out="data/papers/${id}.pdf"
  if is_pdf "$out"; then
    echo "have  $out"
  else
    missing+=("$id")
  fi
done
(( ${#missing[@]} )) || exit 0

if ! curl -fsS --head --connect-timeout 5 --max-time 10 -o /dev/null "$ARXIV/" 2>/dev/null; then
  echo "skip: $ARXIV is not reachable; not downloading ${missing[*]}." >&2
  echo "      The paper tests skip these papers; to run them, put the PDFs in data/papers/." >&2
  exit 0
fi

failed=()
for id in "${missing[@]}"; do
  out="data/papers/${id}.pdf"
  echo "fetch $out"
  if curl -fsSL --connect-timeout 10 --max-time 120 --retry 2 \
          -o "$out.part" "$ARXIV/pdf/${id}" && is_pdf "$out.part"; then
    mv "$out.part" "$out"
  else
    rm -f "$out.part"
    failed+=("$id")
  fi
  sleep 3                     # be polite to arXiv
done

if (( ${#failed[@]} )); then
  echo "skip: could not download ${failed[*]} (network error, or the reply was not a PDF)." >&2
  echo "      The paper tests skip these papers." >&2
fi
exit 0
