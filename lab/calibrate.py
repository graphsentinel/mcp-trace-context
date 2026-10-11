#!/usr/bin/env python3
"""P5 calibration: an auto-instrumented outbound call OUTSIDE MCP, inside a span, in each server image.
Pass = the downstream's received parent is the instrumentation's own client span (O), which is a child of the span."""
import json, os, subprocess, sys, time

CTX, NS = os.environ.get("KUBE_CONTEXT", "k3d-oa-trace-context"), "oa-lab"
ENV = ("OTEL_PROPAGATORS=tracecontext,baggage OTEL_TRACES_SAMPLER=parentbased_always_on "
       "OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.oa-obs:4318 OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf "
       "OTEL_TRACES_EXPORTER=otlp OTEL_METRICS_EXPORTER=none OTEL_LOGS_EXPORTER=none OTEL_BSP_SCHEDULE_DELAY=200 "
       "OTEL_NODE_RESOURCE_DETECTORS=env,host,os OTEL_SERVICE_NAME=calibration")
PY = ("from opentelemetry import trace; import httpx\n"
      "with trace.get_tracer('cal').start_as_current_span('cal') as s:\n"
      "    httpx.get('http://downstream-api.oa-lab:8080/items/ITEM')\n"
      "    print(format(s.get_span_context().trace_id,'032x'), format(s.get_span_context().span_id,'016x'))\n")
JS = ("import {trace} from '@opentelemetry/api';\n"
      "await trace.getTracer('cal').startActiveSpan('cal', async (s) => {\n"
      "  await fetch('http://downstream-api.oa-lab:8080/items/ITEM');\n"
      "  console.log(s.spanContext().traceId, s.spanContext().spanId); s.end(); });\n"
      "setTimeout(() => process.exit(0), 1500);\n")


def k(*a, inp=None):
    return subprocess.run(["kubectl", "--context", CTX, *a], text=True, capture_output=True, input=inp, check=False).stdout


out = {}
for pod, lang in (("stdio-ts1", "js"), ("stdio-py2", "py")):
    item = f"cal-{lang}-{int(time.time())}"
    if lang == "py":
        cmd = f"cd /app && env {ENV} opentelemetry-instrument python3 -c \"$(cat)\""
        src = PY.replace("ITEM", item)
    else:
        cmd = (f"cd /app && cat > /app/oa-cal.mjs && env {ENV} node --experimental-loader=@opentelemetry/instrumentation/hook.mjs "
               f"--import @opentelemetry/auto-instrumentations-node/register /app/oa-cal.mjs")
        src = JS.replace("ITEM", item)
    so = k("-n", NS, "exec", "-i", pod, "--", "sh", "-c", cmd, inp=src).split()
    trace_id, span_id = (so[-2], so[-1]) if len(so) >= 2 else (None, None)
    time.sleep(3)
    entry = json.loads(k("-n", NS, "exec", "driver", "--", "python3", "-c",
                         f"import urllib.request;print(urllib.request.urlopen('http://downstream-api.oa-lab:8080/_log/{item}').read().decode())"))
    e = entry[0] if entry else {}
    spans = json.loads(k("-n", NS, "exec", "driver", "--", "python3", "-c",
                         "import urllib.request;print(urllib.request.urlopen('http://otel-collector.oa-obs:8081/since/0').read().decode())"))["spans"]
    o = next((s for s in spans if s["span"] == e.get("d_parent")), None)
    ok = bool(e) and e.get("d_trace") == trace_id and o is not None and o["parent"] == span_id and o["kind"] == 3
    out[lang] = {"item": item, "cal_trace": trace_id, "cal_span": span_id, "downstream_parent": e.get("d_parent"),
                 "O": o and {k2: o[k2] for k2 in ("name", "parent", "kind", "service")}, "pass": ok}
os.makedirs(os.path.dirname(os.path.abspath(sys.argv[1])), exist_ok=True)
json.dump(out, open(sys.argv[1], "w"), indent=1)
print(json.dumps({k2: v["pass"] for k2, v in out.items()}))
