#!/usr/bin/env python3
"""The test plan: every cell-mode and control of the published measurement, in one place.

  python3 lab/plan.py list [--runs 5]                      print the plan (one line per entry)
  python3 lab/plan.py run --runs 5 --evidence DIR --state DIR
                                                           write DIR/test-plan.json (complete=false), run every entry,
                                                           record each runner's exit code, set complete=true at the end

scripts/lab.sh test calls 'run', then lab/verdict.py and scripts/summary.py (which checks the plan's completeness,
the runner exit codes and every outcome against lab/expected-outcomes.tsv).

RUNS scales the main cell-modes (the published results use 5); controls keep their fixed run counts.
Groups:
  stage-a   Y0 (no bridge) and Y2 (SEP-2028 prototype) x TS SDK 1.32 / Python SDK 2.3 x stdio, HTTP same, HTTP split
            x OTel none / auto                                         24 cell-modes x RUNS
  controls  P0 (driver -> downstream), P4 (unrelated _meta trace), P5 (calibration outside MCP), P6 (no hook module),
            K1-K5 (carrier variants), B5-B7 (invalid / partial _meta)  fixed counts
  py1       Python SDK 1.30, Y0                                        6 cell-modes x RUNS
  gateway   agentgateway in front of the Y0 server, tracing on / off   12 cell-modes x RUNS + K1-K3
  y2b       design experiment: activate _meta before the MCP handler   6 cell-modes x RUNS
  tap       tap neutrality: hook module loaded but silent (L0), or absent (0)   2 runs each, K4 once
"""
import argparse, json, os, subprocess, sys, time

LAB = os.path.dirname(os.path.abspath(__file__))
CONFIG = "lab"  # run ids start with this label (the published runs use c1 / c2)


def cell(group, Y, S, T, I, M="same", variant="main", tap="1", gw=None, runs=1):
    return {"group": group, "kind": "cell", "Y": Y, "S": S, "T": T, "I": I, "M": M, "variant": variant, "tap": tap,
            "gw": gw, "runs": runs}


def entries(runs):
    e = [{"group": "controls", "kind": "P0", "runs": 1}]
    # stage A: Y0 and Y2, TS SDK 1.32 (ts1) and Python SDK 2.3 (py2); stdio carries _meta only (no header): same mode
    for Y in ("Y0", "Y2"):
        for S in ("ts1", "py2"):
            for I in ("none", "auto"):
                e.append(cell("stage-a", Y, S, "stdio", I, runs=runs))
                for M in ("same", "split"):
                    e.append(cell("stage-a", Y, S, "http", I, M, runs=runs))
    # controls (one run each unless noted)
    for S in ("ts1", "py2"):
        for T in ("stdio", "http"):
            e.append(cell("controls", "Y0", S, T, "auto", variant="P4"))
    for S in ("ts1", "py2"):  # P6: no hook module; HTTP twice (a fresh TS pod may miss the 15 s export window once)
        e.append(cell("controls", "Y0", S, "stdio", "auto", tap="0"))
        e.append(cell("controls", "Y0", S, "http", "auto", tap="0", runs=2))
    for Y in ("Y0", "Y2"):
        for S in ("ts1", "py2"):
            for V in ("K1", "K2", "K3", "K4", "K5"):
                e.append(cell("controls", Y, S, "http", "auto", "split", V))
    for S in ("ts1", "py2"):
        for V in ("B5", "B6", "B7"):
            e.append(cell("controls", "Y2", S, "http", "auto", variant=V))
    e.append({"group": "controls", "kind": "P5", "runs": 1})
    # Python SDK 1.30
    for I in ("none", "auto"):
        e.append(cell("py1", "Y0", "py1", "stdio", I, runs=runs))
        for M in ("same", "split"):
            e.append(cell("py1", "Y0", "py1", "http", I, M, runs=runs))
    # agentgateway (HTTP only)
    for S in ("ts1", "py2"):
        for I in ("none", "auto"):
            for M in ("same", "split"):
                e.append(cell("gateway", "Y1", S, "http", I, M, gw="on", runs=runs))
    for S in ("ts1", "py2"):
        for M in ("same", "split"):
            e.append(cell("gateway", "Y1", S, "http", "auto", M, gw="off", runs=runs))
    for S in ("ts1", "py2"):
        for V in ("K1", "K2", "K3"):
            e.append(cell("gateway", "Y1", S, "http", "auto", "split", V, gw="on"))
    # Y2b (instrumented only)
    for S in ("ts1", "py2"):
        e.append(cell("y2b", "Y2b", S, "stdio", "auto", runs=runs))
        for M in ("same", "split"):
            e.append(cell("y2b", "Y2b", S, "http", "auto", M, runs=runs))
    # tap neutrality
    for S in ("ts1", "py2"):
        e.append(cell("tap", "Y0", S, "http", "auto", "split", tap="L0", runs=2))
        e.append(cell("tap", "Y0", S, "stdio", "auto", "same", tap="L0", runs=2))
        e.append(cell("tap", "Y2", S, "http", "auto", "split", tap="L0", runs=2))
        e.append(cell("tap", "Y2b", S, "http", "auto", "split", tap="L0", runs=2))
        e.append(cell("tap", "Y1", S, "http", "auto", "split", tap="0", gw="on", runs=2))
        e.append(cell("tap", "Y2b", S, "http", "auto", "split", "K4", tap="L0"))
    return e


def run_id_prefix(c):
    """Run id without the -r<N> suffix, as lab/run_cell.py names it."""
    return (f"{CONFIG}-{c['Y']}" + (f"gw{c['gw']}" if c["Y"] == "Y1" else "") + f"-{c['S']}-{c['T']}-{c['I']}-{c['M']}-{c['variant']}"
            + {"1": "", "0": "-notap", "L0": "-tapoff"}[c["tap"]])


def label(x):
    if x["kind"] != "cell":
        return f"{x['group']:8} {x['kind']}"
    return f"{x['group']:8} {run_id_prefix(x)} x{x['runs']}"


def cmd_list(a):
    es = entries(a.runs)
    for x in es:
        print(label(x))
    print(f"{len(es)} entries, {sum(x['runs'] for x in es)} runs")


def cmd_run(a):
    os.makedirs(a.evidence, exist_ok=True)
    os.makedirs(a.state, exist_ok=True)
    es = entries(a.runs)
    plan_file = os.path.join(a.state, "test-plan.json")
    plan = {"runs": a.runs, "config": CONFIG, "evidence": os.path.abspath(a.evidence), "complete": False,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "entries": es}
    json.dump(plan, open(plan_file, "w"), indent=1)  # written before anything runs
    failures = open(os.path.join(a.state, "runner-failures"), "w")
    log = open(os.path.join(a.state, "runner.log"), "w")
    py = sys.executable
    for i, x in enumerate(es, 1):
        if x["kind"] == "P0":
            run = f"{CONFIG}-P0"
            ev = os.path.join(a.evidence, run)
            os.makedirs(ev, exist_ok=True)
            k = ["kubectl", "--context", os.environ.get("KUBE_CONTEXT", "k3d-oa-trace-context"), "-n", "oa-lab"]
            argv = ["sh", "-c", " ".join(k) + f" exec driver -- python3 /lab/driver/driver.py --run {run} --variant P0 && "
                    + " ".join(k) + f" cp driver:/out/{run}.json {ev}/driver.json"]
        elif x["kind"] == "P5":
            argv = [py, os.path.join(LAB, "calibrate.py"), os.path.join(a.evidence, f"{CONFIG}-P5", "calibration.json")]
        else:
            argv = [py, os.path.join(LAB, "run_cell.py"), "--config", CONFIG, "--Y", x["Y"], "--S", x["S"], "--T", x["T"],
                    "--I", x["I"], "--M", x["M"], "--variant", x["variant"], "--tap", x["tap"], "--runs", str(x["runs"]),
                    "--evidence", a.evidence]
            if x["gw"]:
                argv += ["--gw", x["gw"]]
        print(f"[{i}/{len(es)}] {label(x)}", flush=True)
        log.write(f"### {label(x)}\n")
        log.flush()
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        log.write(r.stdout)
        log.flush()
        if r.returncode:
            failures.write(f"{label(x)}: exit {r.returncode}\n")
            failures.flush()
            print(f"    runner exit {r.returncode}: " + " | ".join(r.stdout.strip().splitlines()[-3:]), flush=True)
    plan["complete"] = True
    plan["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    json.dump(plan, open(plan_file, "w"), indent=1)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("--runs", type=int, default=5)
    p = sub.add_parser("run")
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--evidence", required=True)
    p.add_argument("--state", required=True)
    a = ap.parse_args()
    {"list": cmd_list, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
