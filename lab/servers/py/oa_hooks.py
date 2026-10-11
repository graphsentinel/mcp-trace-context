"""OA_TAP=1: read-only boundary tap (JSON lines on stderr, no spans, no header changes).
OA_BRIDGE=1: prototype of the pinned SEP-2028 draft (head ec3bf17): _meta -> outbound HTTP headers."""
import contextvars, json, os, re, sys, time

TAP = os.environ.get("OA_TAP") == "1"
BRIDGE = os.environ.get("OA_BRIDGE") == "1"
ACTIVATE = os.environ.get("OA_BRIDGE") == "activate"  # Y2b design experiment, not SEP text
KEYS = ("traceparent", "tracestate", "baggage")
TP = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
_meta = contextvars.ContextVar("oa_meta", default=None)


def log(**o):
    if TAP:
        sys.stderr.write("OA_TAP " + json.dumps({"t": int(time.time() * 1000), **o}) + "\n")
        sys.stderr.flush()


def dbg(**o):
    sys.stderr.write("OA_BRIDGE " + json.dumps(o) + "\n")
    sys.stderr.flush()


def active():
    try:
        from opentelemetry import trace
        sc = trace.get_current_span().get_span_context()
        return {"trace": format(sc.trace_id, "032x"), "span": format(sc.span_id, "016x")} if sc.is_valid else None
    except Exception:
        return None


def pick(m):
    if m is None:
        return {}
    if hasattr(m, "model_dump"):
        m = m.model_dump(by_alias=True, exclude_none=True)
    return {k: m[k] for k in KEYS if isinstance(m, dict) and k in m}


# --- R-1 / tap (a): hold params._meta for the handler's duration ---------------------------------------------
try:  # Python SDK v2: server middleware
    from mcp.server.lowlevel import server as _lls

    class _OAMiddleware:
        async def __call__(self, ctx, call_next):
            m = pick(ctx.meta)
            log(at="sdk-request", method=ctx.method, meta=m, active=active())
            tok = _meta.set(m)
            try:
                return await call_next(ctx)
            finally:
                _meta.reset(tok)

    if hasattr(_lls.Server, "__init__") and "middleware" in open(_lls.__file__).read():
        _orig_init = _lls.Server.__init__

        class _OAActivate:
            """Y2b: attach the context extracted from _meta BEFORE the SDK's OpenTelemetryMiddleware creates its span."""

            async def __call__(self, ctx, call_next):
                m = pick(ctx.meta)
                if not m:
                    return await call_next(ctx)
                from opentelemetry import context as otctx
                from opentelemetry.propagate import extract
                tok = otctx.attach(extract(m, context=otctx.get_current()))  # _meta keys override the current context
                try:
                    return await call_next(ctx)
                finally:
                    otctx.detach(tok)

        def _init(self, *a, **k):
            _orig_init(self, *a, **k)
            if ACTIVATE:
                self.middleware.insert(0, _OAActivate())
            self.middleware.append(_OAMiddleware())  # after the SDK's OpenTelemetryMiddleware

        _lls.Server.__init__ = _init
    else:  # Python SDK v1: wrap the request dispatcher
        _orig_hr = _lls.Server._handle_request

        async def _hr(self, message, req, *a, **k):
            m = pick(getattr(getattr(req, "params", None), "meta", None))
            log(at="sdk-request", method=getattr(req, "method", None), meta=m, active=active())
            tok = _meta.set(m)
            try:
                return await _orig_hr(self, message, req, *a, **k)
            finally:
                _meta.reset(tok)

        _lls.Server._handle_request = _hr
except Exception as e:  # never break the server
    sys.stderr.write(f"OA_HOOKS request hook failed: {e!r}\n")

# --- tap (b): inbound HTTP headers ---------------------------------------------------------------------------
try:
    from starlette.applications import Starlette

    _orig_call = Starlette.__call__

    async def _call(self, scope, receive, send):
        if scope.get("type") == "http":
            h = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            log(at="http-inbound", path=scope.get("path"), headers={k: h[k] for k in KEYS if k in h})
        return await _orig_call(self, scope, receive, send)

    Starlette.__call__ = _call
except Exception as e:
    sys.stderr.write(f"OA_HOOKS inbound hook failed: {e!r}\n")


# --- tap (c) + R-2..R-5: outbound, below httpx (and below OTel's httpx injection) -------------------------------
def _valid(v):
    return isinstance(v, str) and len(v) <= 256 and all(0x20 <= ord(c) <= 0x7E for c in v)


try:
    import httpcore

    _orig_req = httpcore.AsyncConnectionPool.handle_async_request

    async def _handle(self, request):
        hs = [(k.decode(), v.decode()) for k, v in request.headers]
        log(at="http-outbound", path=request.url.target.decode(), active=active(),
            headers={k: v for k, v in hs if k.lower() in KEYS})
        m = _meta.get()
        if BRIDGE and m and len(json.dumps(m)) <= 8192:
            m = {k: v for k, v in m.items() if _valid(v)}  # R-3: invalid fields dropped silently
            if m.get("traceparent") and TP.match(m["traceparent"]):  # R-4 clear-and-use-meta
                had = [(k, v) for k, v in hs if k.lower() in ("traceparent", "tracestate")]
                hs = [(k, v) for k, v in hs if k.lower() not in ("traceparent", "tracestate")]
                hs.append(("traceparent", m["traceparent"]))
                if m.get("tracestate"):
                    hs.append(("tracestate", m["tracestate"]))
                if had:
                    dbg(override="trace-context", replaced=had)
            if m.get("baggage"):  # R-5 prefer-meta
                had = [(k, v) for k, v in hs if k.lower() == "baggage"]
                hs = [(k, v) for k, v in hs if k.lower() != "baggage"] + [("baggage", m["baggage"])]
                if had:
                    dbg(override="baggage", replaced=had)
            request.headers = [(k.encode(), v.encode()) for k, v in hs]
        return await _orig_req(self, request)

    httpcore.AsyncConnectionPool.handle_async_request = _handle
except Exception as e:
    sys.stderr.write(f"OA_HOOKS outbound hook failed: {e!r}\n")
