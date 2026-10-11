"""Pre-registered expected graph per cell. Written to evidence/<run>/expected.json before each run."""


def graph(cell):
    Y, S, T, I = cell["Y"], cell["S"], cell["T"], cell["I"]
    exists = {
        "G": Y == "Y1" and cell.get("gw_tracing", "on") == "on",
        "A": T == "http" and I == "auto",          # transport server span for the tools/call POST
        "S": S == "py2" and I == "auto",            # MCP server span (Python SDK v2 middleware)
        "O": I == "auto",                           # server outbound HTTP client span
        "D": True,
    }
    # Reference parent: the closest existing span upstream in the reference chain
    ref = {}
    if exists["G"]:
        ref["G"] = "SENT"                           # the context the gateway received
    if exists["A"]:
        ref["A"] = "G" if exists["G"] else "HDR"    # inbound HTTP header context
    if exists["S"]:
        ref["S"] = "G" if exists["G"] else "META"   # _meta context (semconv); link to A expected if A exists
    if exists["O"]:
        ref["O"] = "S" if exists["S"] else ("A" if exists["A"] else ("G" if exists["G"] else "META"))
        if Y == "Y2b":  # pre-registered: the activated _meta context is the handler's context
            ref["O"] = "S" if exists["S"] else "META"
    for cand in ("O", "S", "A", "G"):
        if exists[cand]:
            ref["D"] = cand
            break
    else:
        ref["D"] = "META"                            # the agent's client span C
    return {"exists": exists, "reference_parent": ref,
            "link_expected": {"S": "A"} if exists["S"] and exists["A"] else {},
            "spec_bypass_possible": Y == "Y2" and (exists["O"] or exists["S"])}
