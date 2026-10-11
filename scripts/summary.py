#!/usr/bin/env python3
"""Check a test run against the plan and the expected outcomes (scripts/lab.sh summary).

usage: summary.py --plan lab/.runtime/test-plan.json --runner-failures lab/.runtime/runner-failures
                  --expected lab/expected-outcomes.tsv [--markdown out.md] <measurements of the run>.tsv

Checks, each a failure when it does not hold:
  - the run reached its end (plan 'complete') and no runner exited with an error;
  - every planned cell has exactly its planned number of runs, and no run is outside the plan;
  - every run of a cell gives, per call and subject, exactly the outcome in lab/expected-outcomes.tsv
    (trace identity, parentage, link, forwarded tracestate / baggage, first divergence, evidence);
  - no (run, call, subject) appears twice, and the run ids of a cell are exactly <cell>-r1 .. -r<planned runs>;
  - 77 (no verdict) only where the expected file says so; a tolerated setup failure only of the one listed kind
    (TOLERATED_FAILURE), at most once per cell, with at least one complete run in that cell;
  - the P0 and P5 controls pass.
Exit status 0 when every check holds, 1 otherwise.
"""
import argparse, collections, csv, json, re, sys

# The only setup failure that may be tolerated (and only in cells marked 77-tolerated in the expected file): the
# server's outbound span O was not exported within the driver's 15 s window, everything else of the run complete.
TOLERATED_FAILURE = re.compile(r"control failed: nonce=True complete=\{'downstream_and_D': True, 'server_O': False\}")  # fullmatch
VALUE = ["trace_identity", "parentage", "link", "fwd_tracestate", "fwd_baggage", "first_divergence", "evidence"]


def load_expected(path):
    exp = collections.defaultdict(dict)     # cell -> {(call, subject): values}
    tolerated = {}
    controls = {}
    lines = [l for l in open(path) if not l.startswith("#")]
    for r in csv.DictReader(lines, delimiter="\t"):
        if r["subject"] == "control":
            controls[r["cell"]] = r["evidence"]
        elif r["subject"] == "run" and r["evidence"] == "77-tolerated":
            tolerated[r["cell"]] = r["note"]
        else:
            exp[r["cell"]][(r["call"], r["subject"])] = tuple(r[c] for c in VALUE)
    return exp, tolerated, controls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--runner-failures")
    ap.add_argument("--expected", required=True)
    ap.add_argument("--markdown")
    ap.add_argument("tsv")
    a = ap.parse_args()

    plan = json.load(open(a.plan))
    exp, tolerated, controls = load_expected(a.expected)
    rows = list(csv.DictReader(open(a.tsv), delimiter="\t"))
    cfg = plan["config"]
    by_run = collections.defaultdict(list)
    for r in rows:
        by_run[r["run_id"]].append(r)

    problems, notes = [], []
    if not plan.get("complete"):
        problems.append("the test run did not reach its end (interrupted or aborted)")
    if a.runner_failures:
        try:
            problems += [f"runner failed: {l.strip()}" for l in open(a.runner_failures) if l.strip()]
        except FileNotFoundError:
            pass

    groups = collections.defaultdict(lambda: collections.Counter())
    seen_runs = set()
    lines = []
    for x in plan["entries"]:
        g = groups[x["group"]]
        if x["kind"] in ("P0", "P5"):
            want = [("P0", None)] if x["kind"] == "P0" else [("P5:js", "js"), ("P5:py", "py")]
            ctl = [r for r in rows if r["variant"] == x["kind"] and r["subject"] == "control"]
            seen_runs.update(r["run_id"] for r in ctl)
            for name, lang in want:
                got = [r["evidence"] for r in ctl if lang is None or r["S"] == lang]
                ok = got == [controls.get(name, "PASS")]
                g["controls"] += 1
                g["controls_ok"] += ok
                if not ok:
                    problems.append(f"{name}: expected {controls.get(name, 'PASS')}, got {got or 'nothing'}")
                lines.append(f"| {x['group']} | {name} | 1 | {'PASS' if ok else 'FAIL'} |")
            continue
        prefix = f"{cfg}-" + _name(x)
        name = _name(x)
        want = exp.get(name)
        if want is None:
            problems.append(f"{name}: no expected outcome in {a.expected}")
            continue
        runs = sorted(rid for rid in by_run if rid.rsplit("-r", 1)[0] == prefix)
        seen_runs.update(runs)
        g["cells"] += 1
        g["runs"] += len(runs)
        planned_ids = {f"{prefix}-r{i}" for i in range(1, x["runs"] + 1)}
        cell_ok_ids = True
        if set(runs) != planned_ids:
            cell_ok_ids = False
            missing, unplanned = sorted(planned_ids - set(runs)), sorted(set(runs) - planned_ids)
            problems.append(f"{name}: {len(runs)} of {x['runs']} planned runs measured"
                            + (f"; missing {', '.join(missing)}" if missing else "") + (f"; unplanned {', '.join(unplanned)}" if unplanned else ""))
        complete_runs, tol = 0, 0
        cell_ok = cell_ok_ids
        for rid in runs:
            rr = by_run[rid]
            keys = [(r["call"], r["subject"]) for r in rr]
            dup = sorted(k for k, n in collections.Counter(keys).items() if n > 1)
            if dup:
                problems.append(f"{rid}: duplicate verdict rows for {', '.join(f'call {c} {s}' for c, s in dup)}")
                cell_ok = False
                continue
            if any(r["subject"] == "run" for r in rr):
                why = "; ".join(r["note"] for r in rr if r["subject"] == "run")
                only_runlevel = all(r["subject"] == "run" for r in rr)
                if name in tolerated and tol == 0 and only_runlevel and all(TOLERATED_FAILURE.fullmatch(r["note"]) for r in rr):
                    tol += 1
                    notes.append(f"{rid}: tolerated setup failure ({why})")
                else:
                    problems.append(f"{rid}: no verdict (77): {why}")
                    cell_ok = False
                continue
            got = {(r["call"], r["subject"]): tuple(r[c] for c in VALUE) for r in rr}
            for k in sorted(set(want) | set(got)):
                if k not in got:
                    problems.append(f"{rid} call {k[0]} {k[1]}: missing (expected {'/'.join(want[k])})")
                    cell_ok = False
                elif k not in want:
                    problems.append(f"{rid} call {k[0]} {k[1]}: not expected ({'/'.join(got[k])})")
                    cell_ok = False
                elif got[k] != want[k]:
                    diff = ", ".join(f"{c} {w}->{v}" for c, w, v in zip(VALUE, want[k], got[k]) if w != v)
                    problems.append(f"{rid} call {k[0]} {k[1]}: {diff}")
                    cell_ok = False
                else:
                    g["rows_ok"] += 1
                g["rows"] += 1
            complete_runs += 1
        if name in tolerated and tol and not complete_runs:
            problems.append(f"{name}: only the tolerated setup failure, no complete run")
            cell_ok = False
        g["cells_ok"] += cell_ok
        d = want.get(("0", "D"))
        lines.append(f"| {x['group']} | {name} | {len(runs)}/{x['runs']} | {'match' if cell_ok else 'DIFFERS'}"
                     + (f" (D: {d[0]}/{d[1]}, tracestate {d[3]}, baggage {d[4]})" if d else "") + " |")
    extra = sorted(rid for rid in by_run if rid not in seen_runs and rid not in {f"{cfg}-P0", f"{cfg}-P5"})
    if extra:
        problems.append(f"runs not in the plan: {', '.join(extra[:10])}" + (" ..." if len(extra) > 10 else ""))

    print(f"\nTest run {plan.get('started', '?')} -> {plan.get('finished', 'not finished')}, RUNS={plan['runs']}, "
          f"{len(by_run)} runs, {len(rows)} verdict rows\n")
    print(f"{'group':10} {'cells':>11} {'runs':>6} {'rows matching':>15}")
    for name, g in groups.items():
        if g["controls"]:
            print(f"{name:10} {g['cells_ok']:>4} of {g['cells']:<4} {g['runs']:>6} {g['rows_ok']:>6} of {g['rows']:<6}"
                  f"  controls {g['controls_ok']} of {g['controls']} pass")
        else:
            print(f"{name:10} {g['cells_ok']:>4} of {g['cells']:<4} {g['runs']:>6} {g['rows_ok']:>6} of {g['rows']:<6}")
    for n in notes:
        print("note: " + n)
    if problems:
        print("\nCHECKS FAILED:")
        for p in problems[:200]:
            print("  - " + p)
        if len(problems) > 200:
            print(f"  ... {len(problems) - 200} more")
    else:
        print("\nALL CHECKS PASSED: every planned cell-mode and control gave the published outcome in every run")
    if a.markdown:
        with open(a.markdown, "w") as f:
            f.write(f"Test run {plan.get('started')} -> {plan.get('finished')}, RUNS={plan['runs']}, {len(by_run)} runs\n\n")
            f.write("| group | cell-mode / control | runs | result vs. published |\n|---|---|---|---|\n")
            f.write("\n".join(lines) + "\n\n")
            f.write("Checks: " + ("all passed\n" if not problems else "FAILED\n" + "".join(f"- {p}\n" for p in problems)))
            for n in notes:
                f.write(f"- note: {n}\n")
    return 1 if problems else 0


def _name(x):
    return (f"{x['Y']}" + (f"gw{x['gw']}" if x["Y"] == "Y1" else "") + f"-{x['S']}-{x['T']}-{x['I']}-{x['M']}-{x['variant']}"
            + {"1": "", "0": "-notap", "L0": "-tapoff"}[x["tap"]])


if __name__ == "__main__":
    sys.exit(main())
