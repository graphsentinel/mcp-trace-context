"""Minimal reproduction outside MCP: opentelemetry-sdk start_as_current_span(context=ctx)
keeps the AMBIENT baggage, not the baggage of ctx. Variant B attaches the whole extracted ctx first."""
from opentelemetry import baggage, context, trace
from opentelemetry.propagate import extract
from opentelemetry.sdk.trace import TracerProvider

trace.set_tracer_provider(TracerProvider())
t = trace.get_tracer("repro")
carrier = {"traceparent": "00-11111111111111111111111111111111-2222222222222222-01", "baggage": "oa.src=meta"}
tok = context.attach(baggage.set_baggage("oa.src", "hdr"))  # ambient context, as the HTTP instrumentation leaves it
ctx = extract(carrier)
with t.start_as_current_span("A-as-in-mcp-sdk", context=ctx) as s:  # what mcp 2.3.0's middleware does
    print("A parent trace", format(s.get_span_context().trace_id, "032x")[:8], "| current baggage:", baggage.get_all())
tok2 = context.attach(ctx)  # variant B: activate the extracted context, then start the span
with t.start_as_current_span("B-attach-ctx") as s:
    print("B parent trace", format(s.get_span_context().trace_id, "032x")[:8], "| current baggage:", baggage.get_all())
context.detach(tok2); context.detach(tok)
