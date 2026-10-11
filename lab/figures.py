#!/usr/bin/env python3
"""Span-tree figures straight from the evidence (for reading a run at a glance). Stdlib only.

  lab/figures.py            → results/figures/<name>.svg for the runs in FIGS

One lane per service (driver · gateway · MCP server · downstream API); one box per span, filled by trace identity
(agent's _meta trace / header trace / a trace the driver never sent); arrows = parent edges; a dashed red arrow points
to a parent span id that no exported span has. The downstream box lists the trace headers the API received.
Internal ASGI sub-spans (kind INTERNAL) are omitted."""
import html, json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIGS = [
    ("01-ts-http-split", "c1-Y0-ts1-http-auto-split-main-r1", "TS SDK 1.32, Streamable HTTP, OTel auto, header ≠ _meta"),
    ("02-py2-http-split", "c1-Y0-py2-http-auto-split-main-r1", "Python SDK 2.3, Streamable HTTP, OTel auto, header ≠ _meta"),
    ("03-ts-stdio", "c1-Y0-ts1-stdio-auto-same-main-r1", "TS SDK 1.32, stdio, OTel auto"),
    ("04-py2-stdio", "c1-Y0-py2-stdio-auto-same-main-r1", "Python SDK 2.3, stdio, OTel auto"),
    ("05-y2-py2-stdio", "c1-Y2-py2-stdio-auto-same-main-r1", "SEP-2028 prototype (replace below OTel), Python SDK 2.3, stdio"),
    ("06-y2b-py2-http-split", "c2-Y2b-py2-http-auto-split-main-r1", "Design experiment: activate _meta before the MCP handler, Python SDK 2.3"),
    ("07-gw-on-ts-split", "c2-Y1gwon-ts1-http-auto-split-main-r1", "agentgateway 1.6.0 (tracing on), TS SDK 1.32, header ≠ _meta"),
    ("08-gw-off-py2-split", "c2-Y1gwoff-py2-http-auto-split-main-r1", "agentgateway 1.6.0 (no tracing), Python SDK 2.3, header ≠ _meta"),
]
LANES = ["driver (agent)", "agentgateway", "MCP server", "downstream API"]
COL = {"META": ("#cfe3ff", "#1f5fbf"), "HEADER": ("#ffe1c2", "#c26100"), "NEW": ("#e6e6e6", "#666666")}
W, LANE_W, BOX_W, BOX_H, ROW_H, TOP = 1180, 280, 250, 46, 62, 96


def lane_of(service):
    if service == "oa-driver":
        return 0
    if (service or "").startswith("agentgateway"):
        return 1
    if service == "downstream-api":
        return 3
    return 2


def ident(trace, ids):
    if trace == ids.get("Tm"):
        return "META"
    if ids.get("Th") and trace == ids.get("Th"):
        return "HEADER"
    return "NEW"


def role(s, ids):
    if s["span"] == ids.get("R"):
        return "R root"
    if s["span"] == ids.get("C"):
        return "C _meta context"
    if s["span"] == ids.get("Ch"):
        return "Ch header context"
    if s["service"] == "downstream-api":
        return "D"
    if lane_of(s["service"]) == 1:
        return "G " + ("client" if s["kind"] == 3 else "server")
    if s["kind"] == 3:
        return "O outbound"
    if s["name"].startswith("tools/call"):
        return "S MCP span"
    return "A HTTP span"


def figure(run, title):
    d = os.path.join(ROOT, "evidence", "measured", run)
    rec = json.load(open(os.path.join(d, "driver.json")))
    call = rec["calls"][0]
    ids = call["ids"]
    traces = {ids.get("Tm"), ids.get("Th")} - {None}
    entry = [e for e in rec["downstream"][call["item"]] if e["nonce"] in json.dumps(rec["responses"])][0]
    traces.add(entry["d_trace"])
    keep = [s for s in rec["spans"] if s["kind"] != 1 or s["service"] == "oa-driver"]
    # spans of this call: in a call trace, or the outbound span of this call and its server ancestors
    sel = {s["span"]: s for s in keep if s["trace"] in traces and not (s["kind"] == 2 and s["name"].startswith(("initialize", "notifications")))}
    sel = sorted(sel.values(), key=lambda s: (int(s["start"]), lane_of(s["service"])))
    pos = {}
    byid = {x["span"]: x for x in sel}
    for i, s in enumerate(sel):
        ln = lane_of(s["service"])
        y = TOP + i * ROW_H
        x = 20 + ln * LANE_W
        pos[s["span"]] = (x, y)
    H = TOP + len(sel) * ROW_H + 150
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="DejaVu Sans, Arial" font-size="12">',
           '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
           '<path d="M0,0 L10,5 L0,10 z" fill="#333"/></marker>'
           '<marker id="r" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
           '<path d="M0,0 L10,5 L0,10 z" fill="#c62828"/></marker></defs>',
           f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
           f'<text x="20" y="28" font-size="16" font-weight="bold">{html.escape(title)}</text>',
           f'<text x="20" y="48" fill="#555">run {html.escape(run)} · boxes = spans, arrows = parent edges, '
           'fill = trace identity</text>']
    for i, name in enumerate(LANES):
        x = 20 + i * LANE_W
        out.append(f'<rect x="{x - 6}" y="{TOP - 26}" width="{BOX_W + 12}" height="{H - TOP - 110}" fill="#fafafa" stroke="#e0e0e0"/>')
        out.append(f'<text x="{x}" y="{TOP - 10}" font-weight="bold" fill="#333">{html.escape(name)}</text>')
    for s in sel:  # edges first; edges that skip a lane run through the gap left of the target lane (no box crossing)
        if s["parent"] is None:
            continue
        x, y = pos[s["span"]]
        if s["parent"] in pos:
            px, py = pos[s["parent"]]
            if px == x:  # same lane: straight down
                d = f"M{px + BOX_W / 2},{py + BOX_H} L{x + BOX_W / 2},{y}"
            elif px < x:  # to the right: along the parent's row, then down the gap, then into the box
                gap = x - 14
                d = f"M{px + BOX_W},{py + BOX_H / 2} H{gap} V{y + BOX_H / 2} H{x}"
            else:  # to the left
                gap = x + BOX_W + 14
                d = f"M{px},{py + BOX_H / 2} H{gap} V{y + BOX_H / 2} H{x + BOX_W}"
            out.append(f'<path d="{d}" stroke="#333" stroke-width="1.4" fill="none" marker-end="url(#a)"/>')
        else:
            out.append(f'<path d="M{x + BOX_W / 2},{y - 14} L{x + BOX_W / 2},{y}" stroke="#c62828" stroke-dasharray="4 3" '
                       f'stroke-width="1.6" marker-end="url(#r)"/>')
            out.append(f'<text x="{x + BOX_W / 2 + 6}" y="{y - 4}" fill="#c62828" font-size="10">parent {s["parent"][:8]} not exported</text>')
    for s in sel:
        x, y = pos[s["span"]]
        fill, stroke = COL[ident(s["trace"], ids)]
        out.append(f'<rect x="{x}" y="{y}" width="{BOX_W}" height="{BOX_H}" rx="6" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>')
        prole = "root" if s["parent"] is None else (role(byid[s["parent"]], ids).split()[0] if s["parent"] in byid else "not exported")
        out.append(f'<text x="{x + 8}" y="{y + 18}" font-weight="bold">{html.escape(role(s, ids))}'
                   f'<tspan dx="6" font-weight="normal" fill="#555">· {html.escape(s["name"][:18])}</tspan></text>')
        out.append(f'<text x="{x + 8}" y="{y + 36}" fill="#333" font-size="11">trace {s["trace"][:6]} · parent {html.escape(prole)}</text>')
    hdr = {k.lower(): v for k, v in entry["headers"] if k.lower() in ("traceparent", "tracestate", "baggage")}
    yb = H - 116
    out.append(f'<rect x="{20 + 3 * LANE_W - 6}" y="{yb}" width="{BOX_W + 12}" height="78" fill="#fff8e1" stroke="#b28704"/>')
    out.append(f'<text x="{20 + 3 * LANE_W}" y="{yb + 16}" font-weight="bold">API received</text>')
    for j, k in enumerate(("traceparent", "tracestate", "baggage")):
        v = hdr.get(k, "—")
        v = (v[:3] + v[3:9] + "…" + v[-20:]) if k == "traceparent" and len(v) > 40 else v
        out.append(f'<text x="{20 + 3 * LANE_W}" y="{yb + 34 + j * 15}" font-size="11">{k}: {html.escape(v)}</text>')
    lx = 20
    for k, (fill, stroke) in COL.items():
        lab = {"META": "agent's _meta trace", "HEADER": "HTTP header trace", "NEW": "trace the agent never sent"}[k]
        out.append(f'<rect x="{lx}" y="{yb}" width="16" height="12" fill="{fill}" stroke="{stroke}"/>'
                   f'<text x="{lx + 22}" y="{yb + 10}">{lab}</text>')
        lx += 200
    out.append("</svg>")
    return "\n".join(out)


def main():
    od = os.path.join(ROOT, "results", "figures")
    os.makedirs(od, exist_ok=True)
    for name, run, title in FIGS:
        open(os.path.join(od, f"{name}.svg"), "w").write(figure(run, title))
        print(name)


if __name__ == "__main__":
    main()
