"""Sidecar next to the Collector: serves the file exporter's JSONL (the evidence) to the driver.

GET /size           → {"size": <bytes>}
GET /since/<offset> → {"size": <bytes>, "spans": [flattened spans written after <offset>]}
"""
import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PATH = os.environ.get("SPANS_FILE", "/data/spans.jsonl")


def flatten(line):
    out = []
    for rs in json.loads(line).get("resourceSpans", []):
        svc = next((a["value"].get("stringValue") for a in rs.get("resource", {}).get("attributes", [])
                    if a["key"] == "service.name"), None)
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                out.append({"service": svc, "scope": ss.get("scope", {}).get("name"), "name": s.get("name"),
                            "trace": s.get("traceId"), "span": s.get("spanId"), "parent": s.get("parentSpanId") or None,
                            "kind": s.get("kind"), "start": s.get("startTimeUnixNano"), "end": s.get("endTimeUnixNano"),
                            "links": [{"trace": l.get("traceId"), "span": l.get("spanId")} for l in s.get("links", [])],
                            "attrs": {a["key"]: next(iter(a["value"].values()), None) for a in s.get("attributes", [])}})
    return out


class H(BaseHTTPRequestHandler):
    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        size = os.path.getsize(PATH) if os.path.exists(PATH) else 0
        p = self.path.strip("/").split("/")
        if p == ["size"]:
            return self._send({"size": size})
        if len(p) == 2 and p[0] == "since":
            spans, off = [], int(p[1])
            if size > off:
                with open(PATH, "rb") as f:
                    f.seek(off)
                    data = f.read(size - off)
                cut = data.rfind(b"\n") + 1  # only complete lines
                size = off + cut
                for line in data[:cut].splitlines():
                    if line.strip():
                        spans += flatten(line)
            return self._send({"size": size, "spans": spans})
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8081), H).serve_forever()
