#!/usr/bin/env python3
"""Tap neutrality: verdicts of runs with the tap silent (-tapoff) or the module absent (-notap) vs the tapped main
runs of the same cell (any config). Prints per cell: identical / differs."""
import collections, csv, re
# runs that did not measure what their label says (evidence/measured/known-defects.tsv) are left out
DEFECTS = {d["run_id"] for d in csv.DictReader(open("evidence/measured/known-defects.tsv"), delimiter="\t")}
rows = [r for r in csv.DictReader(open("results/measurements.tsv"), delimiter="\t") if r["run_id"] not in DEFECTS]
def key(r):  # cell identity without config, run number and tap suffix
    return (r["Y"], r["S"], r["T"], r["I"], r["M"], r["variant"], r["call"], r["subject"])
def v(r):
    return (r["trace_identity"], r["parentage"], r["link"], r["evidence"], r["fwd_tracestate"], r["fwd_baggage"])
ref = collections.defaultdict(set)
for r in rows:
    if r["config"] in ("c1", "c2") and "notap" not in r["run_id"] and "tapoff" not in r["run_id"]:
        ref[key(r)].add(v(r))
res = collections.defaultdict(list)
for r in rows:
    if r["config"] == "c2" and ("tapoff" in r["run_id"] or "notap" in r["run_id"]):
        mode = "tapoff" if "tapoff" in r["run_id"] else "notap"
        cell = re.sub(r"-r\d+$", "", r["run_id"])
        k = key(r)
        same = v(r) in ref[k] if ref[k] else None
        res[(cell, mode)].append((r["subject"], same, v(r)[:2], sorted(ref[k])[:1]))
for (cell, mode), xs in sorted(res.items()):
    st = "IDENTICAL" if all(x[1] for x in xs) else ("NO REFERENCE" if any(x[1] is None for x in xs) else "DIFFERS")
    print(f"{st:12} {mode:6} {cell}")
    if st != "IDENTICAL":
        for x in xs:
            if not x[1]:
                print("     ", x)
