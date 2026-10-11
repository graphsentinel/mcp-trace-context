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
    for cfg in ("c1", "c2", "c3"):
        assert not [r for r in main_rows(cfg) if r["evidence"] == "77"], cfg


def test_every_main_group_identical_in_5_of_5():
    for cfg in ("c1", "c2", "c3"):
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
    # the first OBI run per SDK (OBI attaching to the new pod): no verdict rows, tolerated (lab/make_expected.py)
    *((f"c5-Y3-{S}-http-obi-same-main-r1", 0, x) for S in ("ts1", "py2") for x in ("A", "O", "D")),
}  # c2 Y1 K1/K3 A agree since the independent A rule accepts the gateway's tools/call client span as parent (2026-10-12)


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
    planned = {plan.run_id_prefix(x).split("-", 1)[1] for x in plan.entries(5, with_obi=True) if x["kind"] == "cell"}
    assert published == planned, (published ^ planned)


def test_expected_outcomes_file_is_current(tmp_path):
    import make_expected
    old = open(make_expected.OUT).read()
    make_expected.OUT = str(tmp_path / "e.tsv")
    make_expected.main()
    assert open(make_expected.OUT).read() == old


def _published_as_test_run(tmp_path, edit=None):
    """The published runs relabelled as a test run (config 'lab'); edit(rows) may change the verdict rows first."""
    import json, plan, re
    lines = open("results/measurements.tsv").read().splitlines()
    rows = [[re.sub(r"^c[1-5]-", "lab-", f[0]), "lab", *f[2:]] for f in (l.split("\t") for l in lines[1:])]
    if edit:
        rows = edit(rows)
    (tmp_path / "m.tsv").write_text("\n".join([lines[0]] + ["\t".join(r) for r in rows]) + "\n")
    (tmp_path / "plan.json").write_text(json.dumps({"runs": 5, "config": "lab", "complete": True, "entries": plan.entries(5, with_obi=True)}))
    r = subprocess.run([sys.executable, "scripts/summary.py", "--plan", str(tmp_path / "plan.json"), "--expected",
                        "lab/expected-outcomes.tsv", str(tmp_path / "m.tsv")], capture_output=True, text=True, check=False)
    failed = [l.strip() for l in r.stdout.split("CHECKS FAILED:")[-1].splitlines() if l.strip().startswith("- ")]
    return r, failed


KNOWN_COUNT_DIFF = ("- Y0-py2-http-auto-same-main-notap: 1 of 2 planned runs measured; "
                    "missing lab-Y0-py2-http-auto-same-main-notap-r2")


def test_summary_accepts_the_published_runs(tmp_path):
    """Every check passes except one: the plan runs the Python P6 HTTP control twice, the published measurement once."""
    r, failed = _published_as_test_run(tmp_path)
    assert failed == [KNOWN_COUNT_DIFF], r.stdout


def test_summary_rejects_a_duplicate_row(tmp_path):
    r, failed = _published_as_test_run(tmp_path, lambda rows: rows + [rows[10]])
    assert r.returncode == 1 and any("duplicate verdict rows" in f for f in failed), r.stdout


def test_summary_rejects_a_changed_outcome(tmp_path):
    def edit(rows):
        k = next(i for i, x in enumerate(rows) if x[0].startswith("lab-Y0-ts1-http-auto-split-main-r1") and x[9] == "D")
        rows[k][10] = "META"  # trace identity HEADER -> META
        return rows
    r, failed = _published_as_test_run(tmp_path, edit)
    assert any("trace_identity HEADER->META" in f for f in failed), r.stdout


def test_summary_tolerates_only_the_listed_setup_failure(tmp_path):
    def edit(rows):
        return [x[:20] + ["control failed: something else"] if x[9] == "run" else x for x in rows]
    r, failed = _published_as_test_run(tmp_path, edit)
    assert any("no verdict (77): control failed: something else" in f for f in failed), r.stdout


def test_summary_rejects_an_unplanned_run(tmp_path):
    def edit(rows):
        extra = [x[:] for x in rows if x[0] == "lab-Y0-ts1-http-auto-split-main-r1"]
        for x in extra:
            x[0] = "lab-Y0-ts1-http-auto-split-main-r6"
        return rows + extra
    r, failed = _published_as_test_run(tmp_path, edit)
    assert any("unplanned lab-Y0-ts1-http-auto-split-main-r6" in f for f in failed), r.stdout


def test_summary_rejects_a_tolerated_failure_with_extra_text_or_duplicate(tmp_path):
    def nl(rows):  # a TSV field cannot hold a newline; extra text after the exact note must not be tolerated
        return [x[:20] + [x[20] + " (retried)"] if x[9] == "run" else x for x in rows]
    r, failed = _published_as_test_run(tmp_path, nl)
    assert any("no verdict (77)" in f for f in failed), r.stdout
    r, failed = _published_as_test_run(tmp_path, lambda rows: rows + [x for x in rows if x[9] == "run"])
    assert any("duplicate verdict rows" in f for f in failed), r.stdout


def test_summary_marks_a_cell_with_a_wrong_run_id(tmp_path):
    def edit(rows):
        for x in rows:
            if x[0] == "lab-Y0-ts1-http-auto-split-main-r5":
                x[0] = "lab-Y0-ts1-http-auto-split-main-r9"
        return rows
    r, failed = _published_as_test_run(tmp_path, edit)
    assert any("missing lab-Y0-ts1-http-auto-split-main-r5; unplanned lab-Y0-ts1-http-auto-split-main-r9" in f for f in failed)
    assert "stage-a      23 of 24" in r.stdout, r.stdout


def test_known_defects_are_documented_and_left_out():
    defects = list(csv.DictReader(open("evidence/measured/known-defects.tsv"), delimiter="\t"))
    assert defects and all(os.path.exists(os.path.join("evidence", "measured", d["evidence"])) for d in defects)
    out = subprocess.run([sys.executable, "lab/compare_tap.py"], capture_output=True, text=True, check=False).stdout
    assert not any(d["run_id"].rsplit("-r", 1)[0] in out for d in defects), out
