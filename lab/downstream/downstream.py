"""downstream-api: the HTTP API a tool calls.

GET /items/<id>  → records the raw request headers, starts span D as a child of the *received* traceparent (or a new
                   root if none/invalid), exports D, returns {"id", "nonce"}.
GET /_log/<id>   → the recorded entries for <id> (the driver reads the raw headers from here).
"""
import json, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/lab/common")
import otlp  # noqa: E402

LOG, LOCK = {}, threading.Lock()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) == 2 and parts[0] == "items":
            start = otlp.now_ns()
            item = parts[1]
            raw = [[k, v] for k, v in self.headers.items()]  # every header line, duplicates kept
            tps = self.headers.get_all("traceparent") or []
            ctx = otlp.parse(tps[0]) if len(tps) == 1 else None
            trace_id, parent = ctx if ctx else (otlp.new_trace(), None)
            d = otlp.new_span()
            nonce = otlp.new_span()
            entry = {"item": item, "headers": raw, "traceparent_count": len(tps), "d_trace": trace_id,
                     "d_span": d, "d_parent": parent, "nonce": nonce}
            with LOCK:
                LOG.setdefault(item, []).append(entry)
            try:
                otlp.export("downstream-api", [otlp.span("GET /items/{id}", trace_id, d, parent, kind=2, start=start,
                                                          attrs={"oa.item": item})])
                entry["export"] = "ok"
            except Exception as e:  # P1 catches a missing span; the response still goes out
                entry["export"] = repr(e)
            return self._send(200, {"id": item, "nonce": nonce})
        if len(parts) == 2 and parts[0] == "_log":
            with LOCK:
                return self._send(200, LOG.get(parts[1], []))
        if parts == ["healthz"]:
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
