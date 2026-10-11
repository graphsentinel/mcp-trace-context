// Loaded with `node --import` AFTER the OTel register hook.
// OA_TAP=1    read-only boundary tap: JSON lines on stderr, no spans, no header changes.
// OA_BRIDGE=1 prototype of the pinned SEP-2028 draft (head ec3bf17): _meta -> outbound HTTP headers (R-1..R-8).
import dc from 'node:diagnostics_channel';
import { AsyncLocalStorage } from 'node:async_hooks';
import { Protocol } from '@modelcontextprotocol/sdk/shared/protocol.js';

const TAP = process.env.OA_TAP === '1';
const BRIDGE = process.env.OA_BRIDGE === '1';
// Y2b (design experiment, not SEP text): activate the _meta context before the request is dispatched; no header edits.
const ACTIVATE = process.env.OA_BRIDGE === 'activate';
const KEYS = ['traceparent', 'tracestate', 'baggage'];
const als = new AsyncLocalStorage();
const log = (o) => { if (TAP) process.stderr.write('OA_TAP ' + JSON.stringify({ t: Date.now(), ...o }) + '\n'); };
const dbg = (o) => process.stderr.write('OA_BRIDGE ' + JSON.stringify(o) + '\n');

let otel = null;
try { otel = await import('@opentelemetry/api'); } catch { /* not installed */ }
const activeCtx = () => {
  const sc = otel?.trace.getSpan(otel.context.active())?.spanContext();
  return sc ? { trace: sc.traceId, span: sc.spanId } : null;
};
const pick = (m) => Object.fromEntries(KEYS.filter((k) => m && k in m).map((k) => [k, m[k]]));

// R-1 / tap (a): hold params._meta for the handler's duration.
const orig = Protocol.prototype._onrequest;
Protocol.prototype._onrequest = function (request, extra) {
  const meta = request?.params?._meta;
  log({ at: 'sdk-request', method: request?.method, meta: pick(meta), active: activeCtx() });
  const run = () => als.run({ meta }, () => orig.call(this, request, extra));
  if (ACTIVATE && otel && meta) {
    const ctx = otel.propagation.extract(otel.context.active(), pick(meta)); // starts from the current context; _meta keys override
    return otel.context.with(ctx, run);
  }
  return run();
};

// tap (b): inbound HTTP headers.
dc.subscribe('http.server.request.start', ({ request }) => {
  log({ at: 'http-inbound', url: request.url, headers: Object.fromEntries(KEYS.filter((k) => request.headers[k]).map((k) => [k, request.headers[k]])) });
});

// R-3 validation (SEP §3.1).
const valid = (v) => typeof v === 'string' && v.length <= 256 && /^[\x20-\x7E]*$/.test(v);
const TP = /^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$/;

function headerList(req) { // undici Request headers: flat array [k, v, ...] or a raw string
  if (Array.isArray(req.headers)) { const o = []; for (let i = 0; i < req.headers.length; i += 2) o.push([String(req.headers[i]), String(req.headers[i + 1])]); return o; }
  return String(req.headers || '').split('\r\n').filter(Boolean).map((l) => { const i = l.indexOf(':'); return [l.slice(0, i), l.slice(i + 1).trim()]; });
}
function setHeaderList(req, list) {
  if (Array.isArray(req.headers)) req.headers = list.flat();
  else req.headers = list.map(([k, v]) => `${k}: ${v}\r\n`).join('');
}

// tap (c) + R-2/R-4/R-5: outbound request, below OTel's own injection (subscribed after it).
dc.subscribe('undici:request:create', ({ request }) => {
  const meta = als.getStore()?.meta;
  let list = headerList(request);
  log({ at: 'http-outbound', path: request.path, active: activeCtx(), headers: Object.fromEntries(list.filter(([k]) => KEYS.includes(k.toLowerCase()))) });
  if (!BRIDGE || !meta) return; // B4: no _meta -> unchanged
  const raw = JSON.stringify(pick(meta));
  if (raw.length > 8192) return; // R-3 total size
  const m = Object.fromEntries(Object.entries(pick(meta)).filter(([, v]) => valid(v))); // invalid fields dropped
  const lower = (k) => k.toLowerCase();
  if (m.traceparent && TP.test(m.traceparent)) { // R-4 clear-and-use-meta, required traceparent
    const had = list.filter(([k]) => ['traceparent', 'tracestate'].includes(lower(k)));
    list = list.filter(([k]) => !['traceparent', 'tracestate'].includes(lower(k)));
    list.push(['traceparent', m.traceparent]);
    if (m.tracestate) list.push(['tracestate', m.tracestate]);
    if (had.length) dbg({ override: 'trace-context', replaced: had });
  }
  if (m.baggage) { // R-5 prefer-meta
    const had = list.filter(([k]) => lower(k) === 'baggage');
    list = list.filter(([k]) => lower(k) !== 'baggage');
    list.push(['baggage', m.baggage]);
    if (had.length) dbg({ override: 'baggage', replaced: had });
  }
  setHeaderList(request, list);
});
