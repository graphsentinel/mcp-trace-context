#!/usr/bin/env python3
"""Per cell-mode summary from results/measurements.tsv (only rows of one config). Every number comes from the TSV."""
import collections, csv, sys

cfg = sys.argv[1] if len(sys.argv) > 1 else "c1"
rows = [r for r in csv.DictReader(open("results/measurements.tsv"), delimiter="\t") if r["config"] == cfg]
cells = collections.OrderedDict()
for r in rows:
    key = (r["Y"], r["S"], r["T"], r["I"], r["M"], r["variant"])
    cells.setdefault(key, collections.defaultdict(collections.Counter))
    if r["subject"] == "run":
        cells[key]["77"][r["note"][:60]] += 1
        continue
    v = f"{r['trace_identity']}/{r['parentage']}"
    if r["subject"] == "S":
        v += f"/link={r['link']}"
    if r["subject"] == "D":
        v += f" ts={r['fwd_tracestate']} bag={r['fwd_baggage']} div={r['first_divergence']}"
    cells[key][r["subject"]][v] += 1
print(f"config {cfg}: {len(rows)} rows, {len(cells)} cell-modes")
for key, subj in cells.items():
    print(" ".join(key))
    for s in ("G", "A", "S", "O", "D", "77"):
        for v, n in subj.get(s, {}).items():
            print(f"   {s:2} {n}x {v}")
