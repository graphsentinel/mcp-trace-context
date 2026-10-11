"""Regression tests over the published evidence (evidence/measured, results/measurements.tsv) and the test plan.
No cluster needed. Run from the repository root:
  docker run --rm -v "$PWD":/src -w /src python:3.14-slim sh -c 'pip install -q pytest && pytest -q lab/tests'"""
import collections, csv, glob, io, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "lab"))
os.chdir(ROOT)
import verdict  # noqa: E402
import verdict_independent  # noqa: E402

KEY = ("run_id", "call", "subject", "trace_identity", "parentage", "link", "evidence", "fwd_tracestate", "fwd_baggage")


def tsv_rows():
    return list(csv.DictReader(open("results/measurements.tsv"), delimiter="\t"))


def regenerated_rows():
    rows = []
    for d in sorted(glob.glob("evidence/measured/*")):
        if d.endswith("-P0") and os.path.exists(d + "/driver.json"):
            rows += verdict.judge_p0(d)
        elif d.endswith("-P5") and os.path.exists(d + "/calibration.json"):
            rows += verdict.judge_p5(d)
        elif os.path.exists(d + "/driver.json") and os.path.exists(d + "/expected.json"):
            rows += verdict.judge(d)
    return rows


def norm(r):
    return tuple("" if r.get(k) is None else str(r.get(k)) for k in KEY)


def test_verdict_reproduces_committed_tsv():
    assert sorted(norm(r) for r in regenerated_rows()) == sorted(norm(r) for r in tsv_rows())


def main_rows(cfg):
    return [r for r in tsv_rows() if r["config"] == cfg and r["variant"] == "main" and "notap" not in r["run_id"]
            and "tapoff" not in r["run_id"]]


def test_no_setup_failure_in_main_cells():
    for cfg in ("c1", "c2"):
        assert not [r for r in main_rows(cfg) if r["evidence"] == "77"], cfg


def test_every_main_group_identical_in_5_of_5():
    for cfg in ("c1", "c2"):
        g = collections.defaultdict(set)
        runs = collections.defaultdict(set)
        for r in main_rows(cfg):
            k = (r["Y"], r["S"], r["T"], r["I"], r["M"], r["subject"])
            g[k].add((r["trace_identity"], r["parentage"], r["link"], r["fwd_tracestate"], r["fwd_baggage"]))
            runs[k[:5]].add(r["run_id"])
        assert all(len(v) == 1 for v in g.values()), cfg
        assert all(len(v) == 5 for v in runs.values()), cfg


def test_spec_bypass_only_with_replacement_prototype():
    for r in tsv_rows():
        if r["parentage"] == "SPEC-BYPASS":
            assert r["Y"] == "Y2" and r["I"] == "auto" and r["subject"] == "D", r["run_id"]


def test_activate_experiment_never_bypasses():
    rows = [r for r in tsv_rows() if r["Y"] == "Y2b" and r["subject"] == "D" and r["variant"] == "main"]
    assert rows and all(r["parentage"] == "EXPECTED" and r["trace_identity"] == "META" for r in rows)


def test_bridge_ran_after_otel_in_every_instrumented_y2_run():
    out = subprocess.run([sys.executable, "lab/audit_hook_order.py"], capture_output=True, text=True, check=False).stdout
    assert out.startswith("Y2 auto runs: 30; bridge-after-OTel proven: 30"), out


KNOWN = {  # the independent implementation differs only here (README: independent verdict check)
    ("c1-Y0-ts1-http-auto-same-main-notap-r1", 0, "A"), ("c1-Y0-ts1-http-auto-same-main-notap-r1", 0, "O"),
    ("c1-Y0-ts1-http-auto-same-main-notap-r1", 0, "D"),
    ("c2-Y1gwon-py2-http-auto-split-K1-r1", 0, "A"), ("c2-Y1gwon-py2-http-auto-split-K3-r1", 0, "A"),
}


def test_independent_implementation_agrees():
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        verdict_independent.main()
    finally:
        sys.stdout = old
    diffs = {(eval(l[5:])[0], eval(l[5:])[1], eval(l[5:])[2]) for l in buf.getvalue().splitlines() if l.startswith("DIFF ")}
    assert diffs == KNOWN, diffs - KNOWN


def test_figures_render_from_evidence():
    import figures
    for name, run, title in figures.FIGS:
        svg = figures.figure(run, title)
        assert svg.startswith("<svg") and "API received" in svg, name


def test_plan_covers_every_published_cell():
    import plan
    published = {r["run_id"].rsplit("-r", 1)[0].split("-", 1)[1] for r in tsv_rows() if r["subject"] != "control"}
    planned = {plan.run_id_prefix(x).split("-", 1)[1] for x in plan.entries(5) if x["kind"] == "cell"}
    assert published == planned, (published ^ planned)


def test_expected_outcomes_file_is_current(tmp_path):
    import make_expected
    old = open(make_expected.OUT).read()
    make_expected.OUT = str(tmp_path / "e.tsv")
    make_expected.main()
    assert open(make_expected.OUT).read() == old


def test_summary_accepts_the_published_runs(tmp_path):
    """The published runs, relabelled as a test run, pass every check except one: the plan runs the Python P6 HTTP
    control twice, the published measurement once."""
    import json, plan, re
    lines = open("results/measurements.tsv").read().splitlines()
    out = [lines[0]] + ["\t".join([re.sub(r"^c[12]-", "lab-", f[0]), "lab", *f[2:]]) for f in (l.split("\t") for l in lines[1:])]
    (tmp_path / "m.tsv").write_text("\n".join(out) + "\n")
    (tmp_path / "plan.json").write_text(json.dumps({"runs": 5, "config": "lab", "complete": True, "entries": plan.entries(5)}))
    r = subprocess.run([sys.executable, "scripts/summary.py", "--plan", str(tmp_path / "plan.json"), "--expected",
                        "lab/expected-outcomes.tsv", str(tmp_path / "m.tsv")], capture_output=True, text=True, check=False)
    failed = [l.strip() for l in r.stdout.split("CHECKS FAILED:")[-1].splitlines() if l.strip().startswith("- ")]
    assert failed == ["- Y0-py2-http-auto-same-main-notap: 1 of 2 planned runs measured"], r.stdout
