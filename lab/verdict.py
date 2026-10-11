#!/usr/bin/env python3
"""Verdicts per run against the pre-registered expected graph (written before each run, lab/expected.py).

  python3 lab/verdict.py                                  evidence/measured -> results/measurements.tsv (published)
  python3 lab/verdict.py --evidence DIR --out FILE.tsv    any other set of runs (scripts/lab.sh test uses this)

Axes per subject (A, S, O, D): trace identity · parentage · link (S) · evidence; downstream forwarded keys.
A run whose controls fail (P2 nonce, completeness) is 77 for every subject."""
import argparse, glob, json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLS = ["run_id", "config", "Y", "S", "T", "I", "M", "variant", "call", "subject", "trace_identity", "parentage", "link",
        "evidence", "fwd_tracestate", "fwd_baggage", "first_divergence", "expected_parent", "observed_parent", "bypassed",
        "note"]


def nonce_ok(rec, call):
    item = call["item"]
    entries = rec["downstream"].get(item) or []
    if not entries:
        return False
    nonces = {e["nonce"] for e in entries}
    return any(isinstance(r, (dict, str)) and any(n in json.dumps(r) for n in nonces) for r in rec["responses"] if r)


def ident(trace, ids):
    if trace is None:
        return "ABSENT"
    if trace == ids.get("Tm"):
        return "META"
    if ids.get("Th") and trace == ids.get("Th"):
        return "HEADER"
    if ids.get("Tx") and trace == ids.get("Tx"):
        return "UNRELATED"
    return "NEW"


def fwd(entry, key, call):
    vals = [v for k, v in entry["headers"] if k.lower() == key]
    if not vals:
        return "ABSENT"
    if len(vals) > 1:
        return "DUPLICATE:" + "|".join(vals)
    v = vals[0]
    if call["meta"].get(key) == v:
        return "META-VALUE" if call["hdr"].get(key) != v else "META=HEADER-VALUE"
    if call["hdr"].get(key) == v:
        return "HEADER-VALUE"
    return "OTHER:" + v


def _url_is(span, item):
    """Exact per-call match: the outbound span's URL attribute ends with /items/<item> (item carries a random suffix)."""
    return any(isinstance(v, str) and v.split("?")[0].endswith(f"/items/{item}") for v in span["attrs"].values())


def find_server_spans(rec, svc, call, all_items=None):
    """Verdict rules: O = the unique client span of this call (exact URL); A/S = the span that is O's parent,
    or a span of the right kind in O's own trace covering O — never 'the first candidate'. Ambiguity → AMBIGUOUS."""
    spans = [s for s in rec["spans"] if s["service"] == svc]
    Os = [s for s in spans if s["kind"] == 3 and _url_is(s, call["item"])]
    out = {"O": None, "S": None, "A": None, "ambiguous": []}
    if len(Os) > 1:
        out["ambiguous"].append("O")
    if len(Os) != 1:
        return out
    O = out["O"] = Os[0]
    o0 = int(O["start"])
    by_id = {(s["trace"], s["span"]): s for s in spans}

    def pick(cands, role):
        par = by_id.get((O["trace"], O["parent"]))
        if par in cands:
            return par
        own = [x for x in cands if x["trace"] == O["trace"]]
        if len(own) == 1:
            return own[0]
        if len(own) > 1:
            out["ambiguous"].append(role)
        # a span of this role in the call's own carrier traces (A in split follows the header, S may follow _meta)
        carriers = {call["ids"].get("Tm"), call["ids"].get("Th")} - {None}
        mine = [x for x in cands if x["trace"] in carriers]
        if len(mine) == 1:
            return mine[0]
        if len(mine) > 1:
            out["ambiguous"].append(role)
        return None

    cover = lambda x: int(x["start"]) <= o0 <= int(x["end"])
    out["S"] = pick([s for s in spans if s["kind"] == 2 and s["name"].startswith("tools/call") and cover(s)], "S")
    out["A"] = pick([s for s in spans if s["kind"] == 2 and not s["name"].startswith(("tools/", "initialize", "notifications"))
                     and cover(s)], "A")
    # S may be in another trace than O only if O is not its child; A likewise — handled by pick's carrier fallback
    return out


def judge(run_dir):
    exp = json.load(open(os.path.join(run_dir, "expected.json")))
    rec = json.load(open(os.path.join(run_dir, "driver.json")))
    cell, g = exp["cell"], exp["graph"]
    svc = f"mcp-{cell['S']}"
    rows = []
    items = [c["item"] for c in rec["calls"]]
    for ci, call in enumerate(rec["calls"]):
        ids = call["ids"]
        base = {"run_id": rec["run"], "config": cell["config"], "Y": cell["Y"] + ({"on": "-gw:on", "off": "-gw:off"}.get(cell.get("gw_tracing"), "")), "S": cell["S"], "T": cell["T"],
                "I": cell["I"], "M": cell["M"], "variant": cell["variant"], "call": ci}
        entries = rec["downstream"].get(call["item"]) or []
        ok = nonce_ok(rec, call) and rec["complete"]["downstream_and_D"] and (rec["complete"]["server_O"] or cell["I"] != "auto")
        if cell["variant"] == "P0":
            ok = bool(entries)
        if not ok:
            rows.append({**base, "subject": "run", "evidence": "77",
                         "note": f"control failed: nonce={nonce_ok(rec, call)} complete={rec['complete']}"})
            continue
        resp = json.dumps(rec["responses"])
        match = [x for x in entries if x["nonce"] in resp]
        if len(entries) != 1 or len(match) != 1:
            rows.append({**base, "subject": "run", "evidence": "77",
                         "note": f"downstream entries={len(entries)} nonce matches={len(match)} (expected exactly one)"})
            continue
        e = match[0]
        found = find_server_spans(rec, svc, call, items)
        span_by_id = {s["span"]: s for s in rec["spans"]}
        role_span = {"META": ids.get("C"), "HDR": ids.get("Ch") or ids.get("C")}
        for r, s in found.items():
            if r != "ambiguous" and s:
                role_span[r] = s["span"]
        # driver-side ids that count as "the sent context"
        first_div = None
        # server-side subjects
        for r in ("A", "S", "O"):
            if not g["exists"][r]:
                continue
            s = found[r]
            if r in found["ambiguous"]:
                rows.append({**base, "subject": r, "evidence": "77", "note": "AMBIGUOUS: more than one candidate span"})
                continue
            if not s:
                rows.append({**base, "subject": r, "evidence": "77", "note": "expected span not found"})
                continue
            exp_parent = role_span.get(g["reference_parent"][r])
            if g["reference_parent"][r] == "G":  # the gateway's tools/call CLIENT span of this call (v2: exact role)
                gcl = [x for x in rec["spans"] if (x["service"] or "").startswith("agentgateway") and x["kind"] == 3
                       and x["name"].startswith("tools/call") and x["trace"] == s["trace"]]
                exp_parent = gcl[0]["span"] if len(gcl) == 1 else None
            if exp_parent is None:
                par = "UNASSESSABLE"  # the reference parent span itself is missing
            elif s["parent"] is None:
                par = "ROOT"  # started a new trace
            else:
                exp_trace = (span_by_id.get(exp_parent) or {}).get("trace") or {ids.get("C"): ids.get("Tm"), ids.get("Ch"): ids.get("Th")}.get(exp_parent)
                par = "EXPECTED" if (s["parent"] == exp_parent and (exp_trace is None or exp_trace == s["trace"])) else (
                "UNASSESSABLE" if s["parent"] not in span_by_id and s["parent"] not in (ids.get("C"), ids.get("Ch")) else "UNEXPECTED")
            link = "n/a"
            if r in g["link_expected"]:
                tgt = found[g["link_expected"][r]]
                link = "PRESENT" if tgt and any(l["span"] == tgt["span"] for l in s["links"]) else "ABSENT"
            idt = ident(s["trace"], ids)
            if first_div is None and idt != "META":
                first_div = f"server:{r}"
            rows.append({**base, "subject": r, "trace_identity": idt, "parentage": par, "link": link, "evidence": "COMPLETE",
                         "expected_parent": f"{g['reference_parent'][r]}={exp_parent}", "observed_parent": s["parent"]})
        # gateway subject (Y1, tracing on): the gateway's tools/call SERVER span — which carrier seeded it
        if g["exists"].get("G"):
            own = {ids.get("Tm"), ids.get("Th")} - {None}
            gs = [x for x in rec["spans"] if (x["service"] or "").startswith("agentgateway") and x["kind"] == 2
                  and x["name"].startswith("tools/call") and x["trace"] in own]
            if not gs:  # the gateway may have started a trace the driver never sent (K1/K3): one call per such run
                gs = [x for x in rec["spans"] if (x["service"] or "").startswith("agentgateway") and x["kind"] == 2
                      and x["name"].startswith("tools/call")] if len(rec["calls"]) == 1 else []
            if not gs:
                rows.append({**base, "subject": "G", "evidence": "77", "note": "gateway tools/call span not found"})
            else:
                x = gs[0]
                seed = {ids.get("C"): "META-SPAN(C)", ids.get("Ch"): "HEADER-SPAN(Ch)"}.get(x["parent"], "OTHER" if x["parent"] else "ROOT")
                rows.append({**base, "subject": "G", "trace_identity": ident(x["trace"], ids), "parentage": seed, "link": "n/a",
                             "evidence": "COMPLETE", "expected_parent": "SENT (header or _meta)", "observed_parent": x["parent"]})
        # downstream subject
        n_tp = e["traceparent_count"]
        if n_tp == 0:
            idt, par, obs = "ABSENT", "n/a", None
        elif n_tp > 1:
            idt, par, obs = "DUPLICATE", "UNEXPECTED", None
        else:
            idt, obs = ident(e["d_trace"], ids), e["d_parent"]
            exp_parent = role_span.get(g["reference_parent"]["D"])
            if exp_parent is None:
                par = "UNASSESSABLE"
            elif obs is None:
                par = "ROOT"
            elif obs == exp_parent:
                par = "EXPECTED"
            elif g["spec_bypass_possible"] and obs == ids.get("C") and e["d_trace"] == ids.get("Tm") and (found["O"] or found["S"]):
                par = "SPEC-BYPASS"
            else:
                par = "UNEXPECTED"
        # v2: only spans on the server's own chain to O are "bypassed" (O and its server ancestors in O's trace)
        chain = []
        if par == "SPEC-BYPASS" and found["O"]:
            x = found["O"]
            while x is not None and x["service"] == svc:
                chain.append(x)
                x = span_by_id.get(x["parent"])
        bypassed = ",".join(f"{x['name'][:12]}={x['span']}" for x in chain)
        if first_div is None and idt != "META":
            first_div = "downstream"
        rows.append({**base, "subject": "D", "trace_identity": idt, "parentage": par, "link": "n/a", "evidence": "COMPLETE",
                     "fwd_tracestate": fwd(e, "tracestate", call), "fwd_baggage": fwd(e, "baggage", call),
                     "first_divergence": first_div or "none", "expected_parent": f"{g['reference_parent']['D']}={role_span.get(g['reference_parent']['D'])}",
                     "observed_parent": obs, "bypassed": bypassed})
    return rows


def judge_p0(d):
    rec = json.load(open(os.path.join(d, "driver.json")))
    c = rec["calls"][0]
    e = (rec["downstream"].get(c["item"]) or [{}])[0]
    got = [v for k, v in e.get("headers", []) if k.lower() == "traceparent"]
    ok = got == [c["hdr"]["traceparent"]] and e.get("d_trace") == c["ids"]["Tm"] and e.get("d_parent") == c["ids"]["C"] \
        and any(s["span"] == e.get("d_span") for s in rec["spans"])
    cfg = rec["run"].split("-")[0]
    return [{"run_id": rec["run"], "config": cfg, "variant": "P0", "subject": "control", "evidence": "PASS" if ok else "FAIL",
             "note": "driver→downstream: header verbatim, D trace/parent, D span in exporter"}]


def judge_p5(d):
    cal = json.load(open(os.path.join(d, "calibration.json")))
    cfg = os.path.basename(d).split("-")[0]
    return [{"run_id": os.path.basename(d), "config": cfg, "variant": "P5", "S": lang, "subject": "control",
             "evidence": "PASS" if v["pass"] else "FAIL", "note": "auto-instrumented outbound outside MCP: downstream parent = O, O parent = calibration span"}
            for lang, v in cal.items()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", default=os.path.join(ROOT, "evidence", "measured"))
    ap.add_argument("--out", default=os.path.join(ROOT, "results", "measurements.tsv"))
    a = ap.parse_args()
    rows = []
    for d in sorted(glob.glob(os.path.join(a.evidence, "*"))):
        if d.endswith("-P0") and os.path.exists(os.path.join(d, "driver.json")):
            rows += judge_p0(d)
        elif d.endswith("-P5") and os.path.exists(os.path.join(d, "calibration.json")):
            rows += judge_p5(d)
        elif os.path.exists(os.path.join(d, "driver.json")) and os.path.exists(os.path.join(d, "expected.json")):
            try:
                rows += judge(d)
            except Exception as ex:
                rows.append({"run_id": os.path.basename(d), "subject": "run", "evidence": "77", "note": f"verdict error {ex!r}"})
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        f.write("\t".join(COLS) + "\n")
        for r in rows:
            f.write("\t".join(str(r.get(c, "") if r.get(c) is not None else "") for c in COLS) + "\n")
    print(len(rows), "rows ->", a.out)


if __name__ == "__main__":
    main()
