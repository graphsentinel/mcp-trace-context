"""Minimal W3C Trace Context + OTLP/JSON span export, stdlib only."""
import json, os, re, secrets, time, urllib.request

TP = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
ENDPOINT = os.environ.get("OTLP_HTTP", "http://otel-collector.oa-obs:4318") + "/v1/traces"


def new_trace():
    return secrets.token_hex(16)


def new_span():
    return secrets.token_hex(8)


def traceparent(trace_id, span_id, flags="01"):
    return f"00-{trace_id}-{span_id}-{flags}"


def parse(tp):
    m = TP.match((tp or "").strip())
    return (m.group(1), m.group(2)) if m else None


def now_ns():
    return time.time_ns()


def span(name, trace_id, span_id, parent=None, kind=1, start=None, end=None, attrs=None):
    a = [{"key": k, "value": {"stringValue": str(v)}} for k, v in (attrs or {}).items()]
    s = {"traceId": trace_id, "spanId": span_id, "name": name, "kind": kind,
         "startTimeUnixNano": str(start or now_ns()), "endTimeUnixNano": str(end or now_ns()), "attributes": a}
    if parent:
        s["parentSpanId"] = parent
    return s


def export(service, spans):
    body = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                               "scopeSpans": [{"scope": {"name": "oa-lab"}, "spans": spans}]}]}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status
