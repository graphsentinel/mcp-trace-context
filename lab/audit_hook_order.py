#!/usr/bin/env python3
"""Hook-order audit: per Y2 run with instrumentation, prove that the bridge ran AFTER OTel's
injection and that nothing overwrote it later: (a) the bridge's OA_BRIDGE override line lists the replaced
traceparent, (b) that replaced value carries the span id of this call's O span (= OTel's own injection), (c) the
downstream received exactly one traceparent = the call's _meta value. Also: the tap's outbound line shows OTel's header."""
import glob, json, os

def lines(d, rec):
    txt = rec.get("server_stderr") or (open(f"{d}/server.log").read() if os.path.exists(f"{d}/server.log") else "")
    return txt.splitlines()

tot = ok = 0
bad = []
for d in sorted(glob.glob("evidence/measured/c[12]-Y2-*-auto-*-main-r*")):
    rec = json.load(open(f"{d}/driver.json")); c = rec["calls"][0]
    svc = "mcp-" + d.split("-Y2-")[1].split("-")[0]
    O = [s for s in rec["spans"] if s["service"] == svc and s["kind"] == 3 and c["item"] in json.dumps(s["attrs"])]
    e = [x for x in rec["downstream"][c["item"]] if x["nonce"] in json.dumps(rec["responses"])]
    L = lines(d, rec)
    item_tap = [l for l in L if "OA_TAP" in l and "http-outbound" in l and c["item"] in l]
    over = [json.loads(l.split("OA_BRIDGE ", 1)[1]) for l in L if "OA_BRIDGE" in l and "trace-context" in l]
    tot += 1
    recv = [v for k, v in e[0]["headers"] if k.lower() == "traceparent"] if len(e) == 1 else None
    replaced_span = None
    for o in over:
        for k, v in o["replaced"]:
            if k.lower() == "traceparent":
                replaced_span = v.split("-")[2]
    cond = (len(O) == 1 and replaced_span == O[0]["span"] and recv == [c["meta"]["traceparent"]] and len(item_tap) == 1
            and O[0]["span"] in item_tap[0])
    ok += cond
    if not cond:
        bad.append((os.path.basename(d), len(O), replaced_span, O[0]["span"] if O else None, recv, len(item_tap), len(over)))
print(f"Y2 auto runs: {tot}; bridge-after-OTel proven: {ok}")
for b in bad[:10]:
    print("NOT PROVEN", b)
