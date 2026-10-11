#!/usr/bin/env python3
"""Independent re-implementation of the verdict rules, written from the rules' description, not from
lab/verdict.py. Purpose: an anti-slop check — if the patched verdict.py hides a bug, the two disagree.

Different method on purpose: instead of selecting spans by time windows, it walks the exported span graph.
- O  = server CLIENT span whose URL ends with /items/<item> (exactly one)
- the server chain = O and its ancestors inside the server's service (by (trace, parent) edges)
- S  = the chain member named "tools/call…", A = the chain member that is a SERVER span and not an MCP method span;
       if A is not on O's chain (Python v2: A follows the header, S follows _meta), A = the server SERVER span whose
       parent is the call's header span (Ch, or C in same mode) or the gateway's client span in that trace
- D  = downstream entry whose nonce is in the call's response (exactly one)
Outputs per (run, call, subject): trace identity, parent role, forwarded keys — compared with results/measurements.tsv.
"""
import csv, glob, json, os, sys

MCP_METHOD = ("tools/", "initialize", "notifications")


def load(run_dir):
    return (json.load(open(os.path.join(run_dir, "expected.json"))), json.load(open(os.path.join(run_dir, "driver.json"))))


def ident(trace, ids):
    for key, label in (("Tm", "META"), ("Th", "HEADER"), ("Tx", "UNRELATED")):
        if ids.get(key) and trace == ids[key]:
            return label
    return "ABSENT" if trace is None else "NEW"


def evaluate(run_dir):
    exp, rec = load(run_dir)
    cell, graph = exp["cell"], exp["graph"]
    svc = f"mcp-{cell['S']}"
    spans = rec["spans"]
    key = {(s["trace"], s["span"]): s for s in spans}
    out = []
    resp = json.dumps(rec["responses"])
    for ci, call in enumerate(rec["calls"]):
        ids = call["ids"]
        entries = [e for e in rec["downstream"].get(call["item"], []) if e["nonce"] in resp]
        if len(entries) != 1:
            out.append((rec["run"], ci, "run", "77"))
            continue
        e = entries[0]
        O = [s for s in spans if s["service"] == svc and s["kind"] == 3 and any(
            isinstance(v, str) and v.split("?")[0].endswith(f"/items/{call['item']}") for v in s["attrs"].values())]
        roles = {}
        if len(O) == 1:
            roles["O"] = O[0]
            x = key.get((O[0]["trace"], O[0]["parent"]))
            while x is not None and x["service"] == svc:  # walk up the server's own chain
                if x["kind"] == 2 and x["name"].startswith("tools/call"):
                    roles.setdefault("S", x)
                elif x["kind"] == 2 and not x["name"].startswith(MCP_METHOD):
                    roles.setdefault("A", x)
                x = key.get((x["trace"], x["parent"]))
        if graph["exists"]["S"] and "S" not in roles and "O" in roles:
            # S not an ancestor of O (should not happen for py2) — look for the tools/call span whose child is O
            pass
        if graph["exists"]["A"] and "A" not in roles:
            gw_client = {s["span"] for s in spans if (s["service"] or "").startswith("agentgateway") and s["kind"] == 3}
            hdr_parents = {ids.get("Ch") or ids.get("C")} | gw_client
            cand = [s for s in spans if s["service"] == svc and s["kind"] == 2 and not s["name"].startswith(MCP_METHOD)
                    and s["parent"] in hdr_parents and s["trace"] in {ids.get("Tm"), ids.get("Th")}]
            if len(cand) == 1:
                roles["A"] = cand[0]
            # tracing-off gateway: A's parent is a span id nobody exported → identify A as the only POST in the call trace
            if not cand:
                cand = [s for s in spans if s["service"] == svc and s["kind"] == 2 and not s["name"].startswith(MCP_METHOD)
                        and s["trace"] in {ids.get("Tm"), ids.get("Th")} and (s["trace"], s["parent"]) not in key]
                if len(cand) == 1:
                    roles["A"] = cand[0]
        named = {ids.get("C"): "C", ids.get("Ch"): "Ch"}
        for r, s in roles.items():
            named[s["span"]] = r
        for s in spans:
            if (s["service"] or "").startswith("agentgateway") and s["span"] not in named:
                named[s["span"]] = "G-client" if s["kind"] == 3 else "G-server"
        named.pop(None, None)
        for r in ("A", "S", "O"):
            if graph["exists"][r]:
                s = roles.get(r)
                if s is None:
                    out.append((rec["run"], ci, r, "77"))
                    continue
                p = s["parent"]
                prole = "ROOT" if p is None else named.get(p, "unexported" if (s["trace"], p) not in key and p not in named else "other")
                out.append((rec["run"], ci, r, ident(s["trace"], ids), prole))
        n_tp = e["traceparent_count"]
        if n_tp == 0:
            out.append((rec["run"], ci, "D", "ABSENT", "none"))
        else:
            out.append((rec["run"], ci, "D", ident(e["d_trace"], ids), named.get(e["d_parent"], "other")))
    return out


# mapping of the independent parent roles onto verdict.py's parentage labels, from the contract's reference table
def expected_label(cell_graph, subject, prole, cell):
    ref = cell_graph["reference_parent"].get(subject)
    want = {"META": "C", "HDR": "Ch" if cell["M"] == "split" else "C", "S": "S", "A": "A", "O": "O", "G": "G-client"}.get(ref)
    if prole == "ROOT":
        return "ROOT"
    if prole == "unexported":
        return "UNASSESSABLE"
    if prole == want:
        return "EXPECTED"
    if subject == "D" and cell_graph["spec_bypass_possible"] and prole == "C":
        return "SPEC-BYPASS"
    return "UNEXPECTED"


def main():
    tsv = {(r["run_id"], int(r["call"]), r["subject"]): r for r in csv.DictReader(open("results/measurements.tsv"), delimiter="\t")
           if r["config"] in ("c1", "c2") and r["call"] != ""}
    agree = disagree = 0
    diffs = []
    for d in sorted(glob.glob("evidence/measured/c[12]-Y*")):
        if not os.path.exists(os.path.join(d, "expected.json")):
            continue
        exp, _ = load(d)
        for row in evaluate(d):
            run, ci, subject = row[:3]
            if row[3] == "77":
                got = ("77",)
            else:
                lab = expected_label(exp["graph"], subject, row[4], exp["cell"]) if subject != "D" or row[3] != "ABSENT" else "n/a"
                got = (row[3], lab)
            t = tsv.get((run, ci, subject))
            ref = ("77",) if t and t["evidence"] == "77" else ((t["trace_identity"], t["parentage"]) if t else ("missing",))
            if got == ref:
                agree += 1
            else:
                disagree += 1
                diffs.append((run, ci, subject, "independent", got, "verdict.py", ref))
    print(f"independent vs verdict.py: agree {agree}, disagree {disagree}")
    for x in diffs[:40]:
        print("DIFF", x)
    return 1 if disagree else 0


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main())
