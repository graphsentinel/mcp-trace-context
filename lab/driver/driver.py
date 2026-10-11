"""Test driver. Stdlib only. One invocation = one run of one cell-mode (or one control).

  driver.py --run RUN --variant V --mode same|split (--url http://.../mcp | --stdio "cmd ...") [--wait-o]
  variants: main · P0 · P4 · K1 (neither) · K2 (header only) · K3 (_meta only) · K4 (two concurrent) · K5 (then none)

Writes /out/<RUN>.json: exact bytes sent (both carriers), responses, downstream log entries, server stderr (stdio),
spans written to the collector after the start offset, and the driver's own span IDs. Verdicts are computed later
on the host against the pre-registered expected graph (lab/verdict.py)."""
import argparse, json, os, subprocess, sys, threading, time, urllib.request

sys.path.insert(0, os.environ.get("OA_COMMON", "/lab/common"))
import otlp  # noqa: E402

DOWN = os.environ.get("DOWNSTREAM", "http://downstream-api.oa-lab:8080")
READER = os.environ.get("READER", "http://otel-collector.oa-obs:8081")
PV = "2025-06-18"


def get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def contexts(variant, mode):
    """Driver span IDs, the _meta trace keys and the HTTP header trace keys for one call."""
    tm, r, c = otlp.new_trace(), otlp.new_span(), otlp.new_span()
    th, ch = otlp.new_trace(), otlp.new_span()
    meta = {"traceparent": otlp.traceparent(tm, c), "tracestate": "oa=meta", "baggage": "oa.src=meta"}
    hdr = dict(meta) if mode == "same" else {"traceparent": otlp.traceparent(th, ch), "tracestate": "oa=hdr",
                                              "baggage": "oa.src=hdr"}
    ids = {"Tm": tm, "R": r, "C": c, "Th": th if mode == "split" else None, "Ch": ch if mode == "split" else None}
    if variant == "K1":
        meta, hdr = {}, {}
    elif variant == "K2":
        meta = {}
    elif variant == "K3":
        hdr = {}
    elif variant == "B5":  # invalid _meta.traceparent → group skipped, request unchanged
        meta = {"traceparent": "00-zzzz-not-a-traceparent-01", "tracestate": "oa=meta", "baggage": "oa.src=meta"}
    elif variant == "B6":  # tracestate without traceparent → trace-context group skipped (required)
        meta = {"tracestate": "oa=meta-only", "baggage": "oa.src=meta"}
    elif variant == "B7":  # CR/LF in a _meta value → field dropped (R-3), no header injection
        meta = {"traceparent": otlp.traceparent(tm, c), "tracestate": "oa=meta", "baggage": "oa.src=meta\r\nX-Injected: yes"}
    elif variant == "P4":  # unrelated trace in _meta that the driver never exports
        tx = otlp.new_trace()
        meta = {"traceparent": otlp.traceparent(tx, otlp.new_span()), "tracestate": "oa=meta", "baggage": "oa.src=meta"}
        ids["Tx"] = tx
    return ids, meta, hdr


def call_msg(i, item, meta):
    params = {"name": "fetch_item", "arguments": {"id": item}}
    if meta:
        params["_meta"] = meta
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": params}


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": PV, "capabilities": {}, "clientInfo": {"name": "oa-driver", "version": "0.1"}}}
INITED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


SESSION = {}


def http_post(url, msg, hdr, sent):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": PV, **hdr}
    if SESSION.get("id"):  # a gateway (agentgateway) issues a session id on initialize; stateless servers do not
        headers["Mcp-Session-Id"] = SESSION["id"]
    body = json.dumps(msg).encode()
    sent.append({"headers": headers, "body": body.decode()})
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            sid = r.headers.get("Mcp-Session-Id")
            if sid and msg.get("method") == "initialize":
                SESSION["id"] = sid
            return {"status": r.status, "body": raw, "session": sid}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read().decode()}


def run_http(url, calls, sent):
    """calls: list of (item, meta, hdr). initialize/initialized carry no trace context."""
    for _ in range(30):  # Service endpoint may lag the rollout: retry connection refused only (setup, not a result)
        try:
            urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=2)
            break
        except urllib.error.HTTPError:
            break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.5)
    out = [http_post(url, INIT, {}, sent), http_post(url, INITED, {}, sent)]
    res = [None] * len(calls)

    def one(k, item, meta, hdr):
        res[k] = http_post(url, call_msg(10 + k, item, meta), hdr, sent)

    ts = [threading.Thread(target=one, args=(k, *c)) for k, c in enumerate(calls)]
    for t in ts:
        t.start()
        if len(calls) > 1 and os.environ.get("OA_SEQ") == "1":
            t.join()
    for t in ts:
        t.join()
    return out + res


def run_stdio(cmd, calls, sent):
    import tempfile
    errf = tempfile.TemporaryFile(mode="w+")
    p = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errf, text=True)
    msgs = [INIT, INITED] + [call_msg(10 + k, item, meta) for k, (item, meta, _) in enumerate(calls)]
    want = {1} | {10 + k for k in range(len(calls))}
    got = {}
    for m in msgs[:2]:
        sent.append({"stdio": json.dumps(m)})
        p.stdin.write(json.dumps(m) + "\n")
        p.stdin.flush()
        if "id" in m:
            while True:
                line = p.stdout.readline()
                if not line:
                    break
                o = json.loads(line)
                if o.get("id") in want:
                    got[o["id"]] = o
                    break
    for m in msgs[2:]:  # K4: both calls written before any response is read
        sent.append({"stdio": json.dumps(m)})
        p.stdin.write(json.dumps(m) + "\n")
        p.stdin.flush()
    deadline = time.time() + 30
    while time.time() < deadline and not want <= set(got):
        line = p.stdout.readline()
        if not line:
            break
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("id") in want:
            got[o["id"]] = o
    p.stdin.close()  # server exits → its exporter flushes on shutdown
    try:
        p.wait(timeout=20)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    errf.seek(0)
    return [got.get(i) for i in sorted(want)], errf.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--variant", default="main")
    ap.add_argument("--mode", default="same", choices=["same", "split"])
    ap.add_argument("--url")
    ap.add_argument("--stdio")
    ap.add_argument("--server-service", default="")
    ap.add_argument("--wait-o", action="store_true", help="wait for the server's outbound client span (I=auto)")
    ap.add_argument("--wait-gw", action="store_true", help="wait for the gateway's tools/call client spans (Y1, tracing on)")
    ap.add_argument("--out", default="/out")
    a = ap.parse_args()

    offset = get_json(f"{READER}/size")["size"]
    t0 = otlp.now_ns()
    rec = {"run": a.run, "variant": a.variant, "mode": a.mode, "url": a.url, "stdio": a.stdio, "calls": [], "sent": []}

    if a.variant == "P0":  # driver → downstream directly
        tp, cp = otlp.new_trace(), otlp.new_span()
        item = f"{a.run}-p0"
        req = urllib.request.Request(f"{DOWN}/items/{item}", headers={"traceparent": otlp.traceparent(tp, cp)})
        rec["sent"].append({"headers": {"traceparent": otlp.traceparent(tp, cp)}, "url": req.full_url})
        rec["responses"] = [urllib.request.urlopen(req, timeout=10).read().decode()]
        otlp.export("oa-driver", [otlp.span("driver P0", tp, cp, start=t0)])
        rec["calls"].append({"item": item, "ids": {"Tm": tp, "C": cp}, "meta": {}, "hdr": {"traceparent": otlp.traceparent(tp, cp)}})
    else:
        n = 2 if a.variant in ("K4", "K5") else 1
        calls = []
        for k in range(n):
            v = "K1" if (a.variant == "K5" and k == 1) else a.variant
            ids, meta, hdr = contexts(v, a.mode)
            if a.stdio:
                hdr = {}
            item = f"{a.run}-{k}-{otlp.new_span()[:6]}"  # unique per invocation (reruns never share a log entry)
            rec["calls"].append({"item": item, "ids": ids, "meta": meta, "hdr": hdr, "variant": v})
            calls.append((item, meta, hdr))
        if a.variant == "K5":  # sequential: context first, then none
            os.environ["OA_SEQ"] = "1"
        if a.stdio:
            rec["responses"], rec["server_stderr"] = run_stdio(a.stdio, calls, rec["sent"])
        else:
            rec["responses"] = run_http(a.url, calls, rec["sent"])
        spans = []
        for c in rec["calls"]:  # export driver spans (R root, C child; Ch root of Th in split)
            i = c["ids"]
            if c.get("variant") in ("K1", "B5", "B6") or a.variant == "P4":
                continue
            spans += [otlp.span("driver run", i["Tm"], i["R"], start=t0),
                      otlp.span("driver tools/call", i["Tm"], i["C"], parent=i["R"], kind=3, start=t0)]
            if i.get("Th"):
                spans.append(otlp.span("driver header-carrier", i["Th"], i["Ch"], kind=3, start=t0))
        if spans:
            otlp.export("oa-driver", spans)

    # downstream entries + spans (completeness: D for every call, O when --wait-o, ≤ 15 s; contract A-7)
    deadline = time.time() + (40 if a.wait_gw else 15)  # the gateway exports in batches; observed > 15 s
    while True:
        rec["downstream"] = {c["item"]: get_json(f"{DOWN}/_log/{c['item']}") for c in rec["calls"]}
        sp = get_json(f"{READER}/since/{offset}")["spans"]
        d_ok = all(any(s["span"] == e["d_span"] for s in sp for e in rec["downstream"][c["item"]])
                   and rec["downstream"][c["item"]] for c in rec["calls"])
        o_ok = (not a.wait_o) or all(any(s["service"] == a.server_service and s["kind"] == 3 and
                                         c["item"] in json.dumps(s["attrs"]) for s in sp) for c in rec["calls"])
        g_ok = (not a.wait_gw) or sum(1 for s in sp if s["service"] == "agentgateway" and s["kind"] == 3
                                       and s["name"].startswith("tools/call")) >= len(rec["calls"])
        if (d_ok and o_ok and g_ok) or time.time() > deadline:
            break
        time.sleep(0.5)
    time.sleep(1.0)  # grace for spans exported in the same batch window
    rec["spans"] = get_json(f"{READER}/since/{offset}")["spans"]
    rec["complete"] = {"downstream_and_D": d_ok, "server_O": o_ok and g_ok, "gateway_G": g_ok}
    rec["t_end_ms"] = int(time.time() * 1000)
    os.makedirs(a.out, exist_ok=True)
    with open(f"{a.out}/{a.run}.json", "w") as f:
        json.dump(rec, f, indent=1)
    print(json.dumps({"run": a.run, "complete": rec["complete"], "n_spans": len(rec["spans"])}))


if __name__ == "__main__":
    main()
