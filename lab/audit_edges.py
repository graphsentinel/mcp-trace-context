#!/usr/bin/env python3
"""Independent audit: for every c1 main run, which span is D's exact parent — matched by full
span id against O / S / A of the same call and the driver's C / Ch — and is D's trace one the driver sent.
Prints counts per cell-mode; does not use the verdict labels."""
import collections, glob, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verdict import find_server_spans  # same span selection, reported separately below

cnt = collections.defaultdict(collections.Counter)
for d in sorted(glob.glob("evidence/measured/c1-Y*-main-r*")):
    if "notap" in d:
        continue
    rec = json.load(open(f"{d}/driver.json")); cell = json.load(open(f"{d}/expected.json"))["cell"]
    for call in rec["calls"]:
        e = [x for x in rec["downstream"][call["item"]] if x["nonce"] in json.dumps(rec["responses"])][0]
        f = find_server_spans(rec, f"mcp-{cell['S']}", call, None)
        names = {k: v for k, v in ((call["ids"].get("C"), "C"), (call["ids"].get("Ch"), "Ch")) if k}
        for r in ("O", "S", "A"):
            if f[r]:
                names[f[r]["span"]] = r
        par = names.get(e["d_parent"], "none" if e["d_parent"] is None else "other")
        tr = "Tm" if e["d_trace"] == call["ids"]["Tm"] else ("Th" if e["d_trace"] == call["ids"].get("Th") else "new")
        # O must be the outbound span of THIS call: its url attribute carries the call's unique item id
        o_ok = (f["O"] is None) or call["item"] in json.dumps(f["O"]["attrs"])
        cnt[" ".join(cell[k] for k in ("Y", "S", "T", "I", "M"))][f"D.parent={par} D.trace={tr} O-matches-call={o_ok}"] += 1
for k, v in sorted(cnt.items()):
    print(k, "|", "; ".join(f"{n}x {x}" for x, n in v.items()))
