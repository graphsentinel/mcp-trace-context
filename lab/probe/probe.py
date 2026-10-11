#!/usr/bin/env python3
"""The two-context test, for any MCP server over Streamable HTTP (stdlib only; scripts/probe.sh runs it in a pod).

One tools/call carries two different W3C trace contexts: one in the HTTP headers (H) and one in params._meta (M), each
with its own tracestate and baggage. The probe then reports which of them reached the API the tool called:

  probe.py --url http://mcp-server.ns:8000/mcp --tool fetch --args '{"url": "{echo}/x"}' --echo 9000
      the probe serves an echo endpoint itself; the tool must call {echo} (the probe pod's address)
  probe.py --url ... --tool fetch_item --args '{"id": "{nonce}"}' --received 'http://api.ns:8080/_log/{nonce}'
      the API records what it received; the probe reads it back (this lab's downstream API does)
  probe.py --url ... --tool NAME --args '{}'
      no view of the API: the probe prints both trace ids to look up in your trace backend

{nonce} in --args / --received is replaced by a unique id; {echo} by http://<probe address>:<--echo port>.
What it answers: which trace the API's request joined (H, M or a new one), whose tracestate and baggage arrived, and
whether the API's parent is one of the probe's own span ids (then nothing in between created a span).
"""
import argparse, json, os, secrets, socket, sys, threading, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PV = "2025-06-18"
KEYS = ("traceparent", "tracestate", "baggage")
RECEIVED = []


def tp(trace, span):
    return f"00-{trace}-{span}-01"


class Echo(BaseHTTPRequestHandler):
    def _any(self):
        RECEIVED.append({k: self.headers.get(k) for k in KEYS})
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _any

    def log_message(self, *a):
        pass


def post(url, body, headers):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Mcp-Protocol-Version": PV,
         **headers}
    with urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), h), timeout=30) as r:
        text = r.read().decode()
        return r.headers.get("mcp-session-id"), text


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", required=True, help="the MCP endpoint (Streamable HTTP)")
    ap.add_argument("--tool", required=True)
    ap.add_argument("--args", default="{}", help="tool arguments as JSON; {nonce} and {echo} are substituted")
    ap.add_argument("--echo", type=int, help="serve an echo endpoint on this port; the tool must call {echo}")
    ap.add_argument("--received", help="URL returning what the API received (JSON list of header maps); {nonce} substituted")
    a = ap.parse_args()

    nonce = secrets.token_hex(6)
    th, ch, tm, cm = secrets.token_hex(16), secrets.token_hex(8), secrets.token_hex(16), secrets.token_hex(8)
    hdr = {"traceparent": tp(th, ch), "tracestate": "probe=hdr", "baggage": "probe.src=hdr"}
    meta = {"traceparent": tp(tm, cm), "tracestate": "probe=meta", "baggage": "probe.src=meta"}
    echo = ""
    if a.echo:
        srv = ThreadingHTTPServer(("0.0.0.0", a.echo), Echo)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        host = os.environ.get("POD_IP") or socket.gethostbyname(socket.gethostname())
        echo = f"http://{host}:{a.echo}"
    args = json.loads(a.args.replace("{nonce}", nonce).replace("{echo}", echo))

    sid, _ = post(a.url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PV, "capabilities": {}, "clientInfo": {"name": "two-context-probe", "version": "1"}}}, {})
    s = {"Mcp-Session-Id": sid} if sid else {}
    post(a.url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, s)
    _, resp = post(a.url, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": a.tool, "arguments": args, "_meta": meta}}, {**s, **hdr})

    print(f"sent  header (H): traceparent {hdr['traceparent']}  tracestate {hdr['tracestate']}  baggage {hdr['baggage']}")
    print(f"sent  _meta  (M): traceparent {meta['traceparent']}  tracestate {meta['tracestate']}  baggage {meta['baggage']}")
    got = list(RECEIVED)
    if a.received:
        with urllib.request.urlopen(a.received.replace("{nonce}", nonce), timeout=10) as r:
            for e in json.load(r):  # a list of header maps, or of {"headers": map or [[name, value], ...]}
                h = e.get("headers", e)
                h = {k.lower(): v for k, v in (h.items() if isinstance(h, dict) else h)}
                got.append({k: h.get(k) for k in KEYS})
    if not (a.echo or a.received):
        print(f"\nno view of the API: search your trace backend for trace {th} (header) and trace {tm} (_meta)")
        print("tool response:", resp[:300])
        return 0
    if not got:
        print("\nthe API received no request from the tool (check --args / {echo})")
        return 1
    for g in got:
        t = g.get("traceparent")
        parts = t.split("-") if t else []
        trace = {th: "H (header)", tm: "M (_meta)"}.get(parts[1], "a NEW trace (neither)") if len(parts) == 4 else "none"
        parent = ""
        if len(parts) == 4:
            parent = {ch: "the probe's header span: no span in between", cm: "the probe's _meta span: no span in between"}.get(
                parts[2], "a span inside your stack")
        src = lambda v, h, m: "H" if v == h else "M" if v == m else ("none" if v is None else f"other ({v})")
        print(f"\nthe API received: traceparent {t}")
        print(f"  trace     {trace}" + (f"; parent: {parent}" if parent else ""))
        print(f"  tracestate {src(g.get('tracestate'), hdr['tracestate'], meta['tracestate'])}   "
              f"baggage {src(g.get('baggage'), hdr['baggage'], meta['baggage'])}")
        mixed = len({x for x in (trace[:1], src(g.get('tracestate'), hdr['tracestate'], meta['tracestate'])[:1],
                                 src(g.get('baggage'), hdr['baggage'], meta['baggage'])[:1]) if x in "HM"}) > 1
        if mixed:
            print("  MIXED: trace and tracestate/baggage come from different carriers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
