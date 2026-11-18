#!/usr/bin/env python
"""Sanity check of a flat ntuple (e.g. from basf2 variablesToNtuple).

Reads every column of one tree and prints a per-column summary (NaN fraction,
range, number of distinct values) with flags for columns that look broken,
followed by a short summary of the flagged columns. Analysis independent:
it only checks that each variable can be read and looks reasonable.

Needs uproot + numpy (physagent or basf2 environment).

usage: python ntupleProduction/inspect_ntuple.py FILE.root [--tree ntuple]
           [--max-entries N] [--match REGEX] [--names-only] [--flagged-only]
           [--expect-constant NAME ...] [--csv summary.csv]
"""
import argparse
import csv
import re
import sys
from collections import Counter

import numpy as np
import uproot

# columns that are legitimately constant in a single-run MC file
EXPECTED_CONSTANT = {"__experiment__", "__run__", "__production__", "__weight__",
                     "Ecms", "beamE", "beamPx", "beamPy", "beamPz"}
COLUMNS = ("dtype", "n", "nan_frac", "n_inf", "min", "mean", "max", "std", "n_distinct", "flags")


def num(x):
    return "nan" if np.isnan(x) else f"{x:.4g}"


def summarize(tree, name, max_entries, expect_constant):
    row = {"name": name, "dtype": "?", "n": 0, "nan_frac": np.nan, "n_inf": 0,
           "min": np.nan, "mean": np.nan, "max": np.nan, "std": np.nan, "n_distinct": 0}
    try:
        arr = tree[name].array(library="np", entry_stop=max_entries)
        if arr.dtype == object:  # vector branches: summarize the flattened values
            parts = [np.asarray(x, dtype=np.float64).ravel() for x in arr]
            vals, row["dtype"] = (np.concatenate(parts) if parts else np.empty(0)), "jagged"
        else:
            vals, row["dtype"] = arr.astype(np.float64, copy=False), str(arr.dtype)
    except Exception as err:  # keep going: an unreadable column is a finding, not a crash
        row["flags"] = f"UNREADABLE ({type(err).__name__})"
        return row

    nan, inf = np.isnan(vals), np.isinf(vals)
    finite = vals[~(nan | inf)]
    row.update(n=vals.size, nan_frac=nan.mean() if vals.size else np.nan, n_inf=int(inf.sum()))
    if finite.size:
        uniq = np.unique(finite)
        row.update(min=uniq[0], mean=finite.mean(), max=uniq[-1], std=finite.std(),
                   n_distinct=uniq.size)

    flags = []
    if not vals.size:
        flags.append("EMPTY")
    elif not finite.size:
        flags.append("ALL_NAN" if nan.all() else "NO_FINITE")
    else:
        if row["nan_frac"] > 0.95:
            flags.append("MOSTLY_NAN")
        if row["n_distinct"] == 1 and name not in expect_constant:
            flags.append("CONSTANT")
    if row["n_inf"]:
        flags.append("HAS_INF")
    row["flags"] = ",".join(flags)
    return row


def overview(f, tree, max_entries):
    print("== File")
    for key, cls in f.classnames().items():
        if cls == "TTree":
            print(f"  {key:<30s} {f[key].num_entries:>12d} entries")
    names = set(tree.keys())
    n_read = tree.num_entries if max_entries is None else min(max_entries, tree.num_entries)
    print(f"Tree '{tree.name}': {tree.num_entries} rows ({n_read} read), {len(names)} columns")
    # basf2 bookkeeping columns, if present
    if {"__candidate__", "__ncandidates__"} <= names:
        cand = tree["__candidate__"].array(library="np", entry_stop=max_entries)
        ncand = tree["__ncandidates__"].array(library="np", entry_stop=max_entries)
        per_event = Counter(ncand[cand == 0].tolist())
        print(f"Events: {sum(per_event.values())}; candidates per event -> events: "
              + ", ".join(f"{k}: {v}" for k, v in sorted(per_event.items())))


def print_table(rows, flagged_only):
    shown = [r for r in rows if r["flags"]] if flagged_only else rows
    print(f"\n== Columns ({len(shown)} shown)")
    if not shown:
        return
    w = max(len(r["name"]) for r in shown)
    print(f"{'name':<{w}}  {'dtype':<7} {'n':>9} {'NaN%':>6} {'inf':>5} {'min':>11} {'mean':>11} "
          f"{'max':>11} {'std':>11} {'distinct':>8}  flags")
    for r in shown:
        print(f"{r['name']:<{w}}  {r['dtype']:<7} {r['n']:>9d} {100 * r['nan_frac']:>6.1f} {r['n_inf']:>5d} "
              f"{num(r['min']):>11} {num(r['mean']):>11} {num(r['max']):>11} {num(r['std']):>11} "
              f"{r['n_distinct']:>8d}  {r['flags']}")


def print_summary(rows):
    by_flag = {}
    for r in rows:
        for flag in filter(None, r["flags"].split(",")):
            by_flag.setdefault(flag.split(" ")[0], []).append(r["name"])
    print(f"\n== Summary: {len(rows)} columns, {sum(not r['flags'] for r in rows)} without flags")
    for flag, names in sorted(by_flag.items()):
        print(f"  {flag:<11s} {len(names):>4d}: {', '.join(names)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("file")
    ap.add_argument("--tree", default="ntuple")
    ap.add_argument("--max-entries", type=int, default=None, help="read only the first N rows")
    ap.add_argument("--match", default=None, help="only look at columns matching this regex")
    ap.add_argument("--names-only", action="store_true", help="just list the column names (no reading)")
    ap.add_argument("--flagged-only", action="store_true", help="print only flagged columns in the table")
    ap.add_argument("--expect-constant", nargs="*", default=[],
                    help="extra columns that are allowed to be constant")
    ap.add_argument("--csv", default=None, help="also write the per-column table to this file")
    args = ap.parse_args()

    with uproot.open(args.file) as f:
        trees = [k.split(";")[0] for k, c in f.classnames().items() if c == "TTree"]
        if args.tree not in trees:
            sys.exit(f"No tree '{args.tree}' in {args.file}. Trees: {', '.join(trees) or 'none'}")
        tree = f[args.tree]
        names = list(tree.keys())
        if args.match:
            names = [n for n in names if re.search(args.match, n)]

        if args.names_only:
            print("\n".join(names))
            return

        overview(f, tree, args.max_entries)
        expect_constant = EXPECTED_CONSTANT | set(args.expect_constant)
        rows = [summarize(tree, n, args.max_entries, expect_constant) for n in names]

    print_table(rows, args.flagged_only)
    print_summary(rows)
    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["name", *COLUMNS])
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nWrote {args.csv}")


if __name__ == "__main__":
    main()
