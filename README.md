# Who owns an agent's trace on Kubernetes? W3C Trace Context across the MCP boundary

A Kubernetes lab that measures whether an MCP tool's outbound HTTP request joins the calling agent's trace, hop by
hop: TypeScript and Python MCP SDKs, stdio and Streamable HTTP, with and without OpenTelemetry auto-instrumentation,
directly and through agentgateway, plus a prototype of the draft SEP-2028 and one alternative design.
Everything runs on a local k3d cluster and can be reproduced from zero with a few commands ([Quickstart](#quickstart)).

Project page: https://graphsentinel.github.io/mcp-trace-context/

- [The problem](#the-problem)
- [Why Kubernetes](#why-kubernetes)
- [What is measured](#what-is-measured)
- [Results](#results)
- [Quickstart](#quickstart)
- [How-to](#how-to)
- [Troubleshooting](#troubleshooting)
- [Repository layout](#repository-layout)

## The problem

An agent calls an MCP tool; the tool calls an API. For one trace to show the whole path, the API's request has to
carry the agent's trace context. MCP has two places to carry it:

- **`_meta`** in the JSON-RPC request. SEP-414 (merged) reserves `traceparent`, `tracestate` and `baggage` there, and
  OpenTelemetry's MCP semantic conventions (Development) take the server span's parent from `_meta`.
- **HTTP headers** on Streamable HTTP, which OpenTelemetry's HTTP instrumentation reads and writes on its own.
  stdio has no headers, so there `_meta` is the only carrier.

The specification does not require a server to forward the `_meta` context to the requests its tools make. The draft
SEP-2028 proposes that server SDKs do it, and that `_meta` replace the headers set by auto-instrumentation.

So within one tool call there can be **two contexts**: the one in `_meta` and the one in the HTTP header. They agree
when the agent's own client instrumentation writes both; they differ when something between agent and server (a
proxy, a gateway, a second instrumentation) rewrites one of them. Which one wins, and whether the server's own spans
stay in the chain, decides what an operator sees in the trace backend.

## Why Kubernetes

On a cluster, an agent's tool call crosses several separately deployed and separately configured pieces: the agent's
pod, an AI gateway, the MCP server's pod and the APIs its tools call. In this lab each is its own pod (driver →
agentgateway → MCP server → downstream API, with an OpenTelemetry Collector), installed by one Helm chart.

The cloud native ways of tracing that path, such as HTTP auto-instrumentation, gateways and eBPF instrumentation,
propagate context in HTTP headers. MCP added a second carrier in the request body (`_meta`). When the two differ, some
layer has to decide which one the next hop sees. Today that decision is being discussed in three places:

- **the MCP server SDK**: draft SEP-2028 would make SDKs forward `_meta` to outbound requests;
- **OpenTelemetry**: the MCP semantic conventions (Development) say which context parents the server span;
- **the cluster**: the Kubernetes WG AI Gateway's
  [payload-processing](https://github.com/kubernetes-sigs/wg-ai-gateway/blob/main/proposals/7-payload-processing.md)
  and [multi-stage](https://github.com/kubernetes-sigs/wg-ai-gateway/blob/main/proposals/62-multi-stage-routing.md)
  proposals (both Proposed) add a declarative way to set headers from body fields. As of 11 October 2026 neither
  mentions trace context.

The lab measures the first two today: released SDKs, the SEP-2028 prototype (Y2), the alternative design (Y2b) and
agentgateway as released (Y1). The cluster option, a gateway policy that copies `_meta` into headers before the MCP
server (Y4), is being added next, together with OpenTelemetry Operator injection and eBPF instrumentation (Y3); for
each, the question is which carrier it reads, writes or drops.

## What is measured

The driver (stdlib Python, in the cluster) plays the agent. Per call it chooses both carriers deliberately and records
the exact bytes sent, every span the Collector received, and the headers the downstream API received.

```
driver ──tools/call──▶ [agentgateway] ──▶ MCP server (one tool) ──GET /items/<id>──▶ downstream API
  _meta trace  (Tm)                          spans A, S, O                             records traceparent,
  header trace (Th)                                                                    tracestate, baggage → D
        └──────────────── all spans ──▶ OpenTelemetry Collector (file exporter = the evidence) ◀───────┘
```

| Dimension | Values |
|---|---|
| SDK (server) | `ts1` MCP TypeScript SDK 1.32.1 · `py2` MCP Python SDK 2.3.0 · `py1` MCP Python SDK 1.30.0 |
| Transport | `stdio` · `http` (Streamable HTTP, stateless, JSON responses) |
| Instrumentation | `none` · `auto` (OpenTelemetry zero-code: `auto-instrumentations-node`, `opentelemetry-instrument`) |
| Mode | `same` (header = `_meta`) · `split` (header and `_meta` carry different traces; HTTP only) |
| Path / design | `Y0` as released · `Y1` agentgateway v1.6.0 in front (tracing on / off) · `Y2` prototype of draft SEP-2028 (pinned at `ec3bf17`: `_meta` replaces the outbound headers, below the instrumentation) · `Y2b` alternative design: activate the `_meta` context before the MCP handler |

**Subjects** (one verdict each, per call): `A` the server's inbound transport span, `S` its MCP `tools/call` span,
`O` the tool's outbound client span, `G` agentgateway's `tools/call` span, `D` the downstream request.
For each subject the verdict records:

- **trace identity**: `META` (the `_meta` trace) · `HEADER` (the header trace) · `NEW` (a trace the driver never sent)
  · `UNRELATED` (P4 control) · `ABSENT` (no `traceparent` at the API);
- **parentage** against a reference parent written **before** each run (`evidence/.../expected.json`, `lab/expected.py`):
  `EXPECTED` · `ROOT` · `UNEXPECTED` · `UNASSESSABLE` (the parent span is not among the collected spans) · `SPEC-BYPASS`
  (D's parent is the agent's client span although the server's own spans exist);
- the **`tracestate` and `baggage` values** the API received, and where the chain first left the `_meta` trace.

`77` means no verdict (a control of the run failed); it is expected only where `lab/expected-outcomes.tsv` says so.

**Controls:** P0 driver → API directly · P4 an unrelated `_meta` trace · P5 an instrumented call outside MCP
(calibration) · P6 the hook module not loaded at all · K1–K5 carrier variants (neither, header only, `_meta` only,
two concurrent calls, a call after a call with context) · B5–B7 invalid or partial `_meta`. Tap neutrality: the
read-only boundary tap silent or absent gives the same verdicts (for the gateway with the module absent, see the
known defect under [Results](#results)).

**Scope.** A selected matrix, not every SDK × transport × instrumentation combination; plaintext HTTP/1.1; a driver
that sets both carriers on purpose. The `Y2` hooks are our reading of a draft, not an implementation by its authors.

## Results

### Sources

| Result | Runs | Stack | Where |
|---|---|---|---|
| Published measurement, stage A (Y0, Y2) | 24 cell-modes × 5 runs + controls (c1: 155 run directories + P0, P5) | Node 22, CPython 3.12, Collector 0.151.0, k3s v1.37.1 | `evidence/measured/c1-*`, rows `config=c1` in `results/measurements.tsv` |
| Published measurement, stage B (Y1, Y2b, py1, tap neutrality) | 24 cell-modes × 5 runs + gateway probes + tap runs (c2: 148) | same | `evidence/measured/c2-*`, rows `config=c2` |
| Reproduction on an updated stack, fresh Ubuntu VMs | Ubuntu 26.04 and 24.04: 306 runs each (RUNS=5), then reinstall + 114 runs | Node 24, CPython 3.14, Collector 0.162.0, k3s v1.37.1 (`scripts/versions.env`) | `evidence/e2e-multipass/` |

The published runs were made before this repository existed, with the lab code of the first commit except for the
runner changes it describes (cluster and images from the environment, gateway upstream per tap mode) and comment
edits; each run's `images.txt` records the image IDs it ran with. Every published number below comes from `results/measurements.tsv`, which `lab/verdict.py` regenerates exactly from
`evidence/measured/` (a test checks it). In the published runs every (cell-mode, call, subject) gave the same verdict
in every complete run (one TS P6 run had no verdict, see `lab/expected-outcomes.tsv`).

**Known defect in the published runs.** The four c2 runs labelled "gateway, hook module absent"
(`c2-Y1gwon-*-http-auto-split-main-notap-r*`) reached the server **with** the module: the gateway's upstream ignored the
tap mode (see `gateway-config.yaml` in those runs). They are kept as recorded. The runner is fixed, and the reproduction
measures the gateway in front of a server without the module.

### Without a bridge (Y0): where the trace ends

| SDK | Transport | OTel | Mode | At the API (D) | `tracestate` / `baggage` at D |
|---|---|---|---|---|---|
| TS 1.32, Python 2.3, Python 1.30 | stdio, http | none | any | **no `traceparent`** | none |
| TS 1.32 | stdio | auto | same | **new trace** (server's outbound span is a root) | none |
| TS 1.32 | http | auto | same | agent's trace, expected parent | as sent |
| TS 1.32 | http | auto | split | **header's trace** | header's |
| Python 2.3 | stdio | auto | same | agent's trace (`_meta`), expected parent | `_meta`'s / **none** |
| Python 2.3 | http | auto | same | agent's trace, expected parent | as sent |
| Python 2.3 | http | auto | split | **`_meta`'s trace** | **`_meta`'s / header's** (mixed) |
| Python 1.30 | stdio / http | auto | same / split | as TS 1.32: new trace on stdio, header's trace in split | |

- **Without OpenTelemetry in the server, none of the three tested SDK versions forwarded `traceparent`**, in either
  transport.
- **When header and `_meta` differ (split), TS SDK 1.32 and Python SDK 1.30 follow the header; Python SDK 2.3 follows
  `_meta` for `traceparent` and `tracestate` but forwards the header's `baggage`.** The mix comes from
  `start_as_current_span(context=ctx)`: the context chooses the parent, but the span is attached to the current
  (header) context, so its baggage stays. `lab/repro_baggage.py` reproduces it with `opentelemetry-sdk` alone.
- Python SDK 2.3's MCP span has the `_meta` parent but **no link** to its transport span (the semantic conventions'
  second SHOULD), in every instrumented HTTP run.

### The SEP-2028 prototype (Y2) and the alternative (Y2b)

| | Uninstrumented server | Instrumented server |
|---|---|---|
| **Y2**: `_meta` replaces the outbound headers | the API joins the agent's trace with the expected parent, both SDKs and transports | agent's trace, but the API's parent is the **agent's client span**: the server's spans leave the chain (**SPEC-BYPASS**), in all 6 instrumented cell-modes |
| **Y2b**: `_meta` activated before the MCP handler | not measured (needs OTel) | agent's trace, the server's **outbound span stays the parent**, `tracestate` and `baggage` from `_meta`: in all 6 cell-modes, no bypass, no baggage mix |

Y2b shows that in these cells the bypass comes from replacing the header below the instrumentation, not from using
`_meta`. It does not show that every activation-based implementation works.

### agentgateway v1.6.0 as released (Y1)

- **Carrier collapse:** at the server, header and `_meta` always carried the same context, the header's. In split
  mode the agent's `_meta` trace never reached the server, with gateway tracing on or off.
- With tracing **on**, the gateway's `tools/call` span is seeded from the header.
- With tracing **off**, the gateway still rewrites the `traceparent` span id (and writes it into `_meta`) but exports
  no span: the server's spans point to a parent span that is not among the spans the lab collected (`UNASSESSABLE`).
- `_meta` only (K3, one run per SDK): the gateway starts a new trace and overwrites `_meta.traceparent`, but leaves the
  agent's `tracestate` and `baggage`; the Python server forwarded that `tracestate` on the new trace.

### Tap neutrality

The servers carry a read-only tap (it logs what crosses the boundary; it creates no spans and changes no headers).
With the tap module loaded but silent (`-tapoff`), the verdicts equal the tapped runs in all 8 published c2 cells that
have a tapped counterpart (`python3 lab/compare_tap.py`); with the module absent (`-notap`, P6) they equal the tapped
runs for both SDKs over stdio and HTTP. Behind the gateway with the module absent, see the known defect above.

### Reproduction on an updated stack

From the committed tree, in fresh multipass VMs (8 CPUs, 16 GB), the quickstart as written, then `lab.sh reinstall`,
one more round, `lab.sh uninstall`:

| VM | Commit | Main round | After reinstall | Evidence |
|---|---|---|---|---|
| Ubuntu 26.04 | `a567fc7` | 306 runs, 864 verdict rows: **every planned cell-mode and control gave the published outcome in every run** | 114 runs, 384 rows: same | [`evidence/e2e-multipass/20261011T174925Z-ubuntu26.04/`](evidence/e2e-multipass/20261011T174925Z-ubuntu26.04/) (`summary.md`, `measurements.tsv`, raw runs in `raw-evidence-*.tar.gz`) |
| Ubuntu 24.04 | `22e5606` | 306 runs, 864 verdict rows: same | 114 runs, 384 rows: same | [`evidence/e2e-multipass/20261011T183123Z-ubuntu24.04/`](evidence/e2e-multipass/20261011T183123Z-ubuntu24.04/) |

The tolerated setup failure did not occur. The gateway cell with the hook module absent, which now reaches the
server without the module (`gateway-config.yaml` of those runs), gave the outcome of the tapped cell in all 16 runs
(2 per SDK per round, in both VMs): tap neutrality holds behind the gateway too.

The reproduction is a regression check on current versions (Node 24, CPython 3.14, Collector 0.162, zod 4 — within
the TypeScript SDK's declared `^3.25 || ^4.0` — and `@opentelemetry/api` 1.9.1; the MCP and OpenTelemetry SDK versions
are unchanged). It does not re-create the published environment, whose versions are listed under
[Versions](#versions) and in each run's `images.txt`. Its plan differs from the published measurement in two
places: the Python P6 HTTP control runs twice (published: once), and the gateway cell with the hook module absent now
reaches the server without the module.

## Quickstart

On Ubuntu 24.04 or 26.04 (amd64 or arm64), 8 CPUs and 16 GB recommended:

```sh
git clone https://github.com/graphsentinel/mcp-trace-context.git
cd mcp-trace-context
scripts/platform.sh install      # Docker (Ubuntu's package), kubectl, k3d, helm; only what is missing
scripts/platform.sh cluster-up   # k3d cluster oa-trace-context
scripts/lab.sh all               # build images, install the lab, run the whole plan (~45 min), check the result
```

`scripts/lab.sh all` ends with `ALL CHECKS PASSED` when every planned cell-mode and control gave the published
outcome in every run, and exits 1 otherwise. `RUNS=1 scripts/lab.sh test` runs one round of the main cell-modes
(about 15 minutes). To remove everything: `scripts/lab.sh uninstall` (the lab) and `scripts/platform.sh cluster-down`
(the cluster).

## How-to

### Two layers

| Layer | Script | Installs | Removed by |
|---|---|---|---|
| Platform | `scripts/platform.sh` | Docker (Ubuntu's `docker.io` + `docker-buildx`, only if no Docker is present), python3, git, curl; kubectl, k3d, helm at the pinned versions (sha256-checked, only if missing); a k3d cluster | `cluster-down` (the cluster); the tools stay |
| Lab | `scripts/lab.sh` | three MCP server images, the Helm release `oa-lab` (namespaces `oa-obs`, `oa-lab`) | `uninstall` |

`platform.sh install` never replaces what is already there; `UPGRADE_TOOLS=1` replaces kubectl, k3d and helm with the
pinned versions.

### Lab commands

| Command | What it does |
|---|---|
| `scripts/lab.sh build` | builds `oa-ts1`, `oa-py1`, `oa-py2` from `lab/servers/` (base images pinned by digest, dependencies from lockfiles) |
| `scripts/lab.sh install` | imports the images into k3d, `helm upgrade --install oa-lab`, waits for the Collector, API and driver |
| `scripts/lab.sh test` | runs the plan (`lab/plan.py`), computes verdicts, then `summary` |
| `scripts/lab.sh summary` | checks the last test against `lab/expected-outcomes.tsv`; writes `lab/.runtime/summary.md` |
| `scripts/lab.sh uninstall` | `helm uninstall`; also removes the MCP servers and gateways the tests started (they live in `oa-lab`) |
| `scripts/lab.sh reinstall` | build + uninstall + install |
| `scripts/lab.sh status` | release and pods |
| `scripts/lab.sh all` | build + install + test |

Options (environment): `RUNS` (5), `KUBE_CONTEXT`, `REGISTRY`, `ENGINE` (`docker` or `podman`, detected).

### The test plan and the expected outcomes

`python3 lab/plan.py list` prints every entry: the 24 stage-A cell-modes, the controls, Python SDK 1.30, the gateway
cell-modes, Y2b and the tap-neutrality runs (102 entries, 306 runs at `RUNS=5`). `lab/expected-outcomes.tsv` holds,
for each entry, call and subject, the outcome of the published measurement; `lab/make_expected.py` regenerates it.
`scripts/summary.py` fails when the run did not finish, a runner exited with an error, a planned run is missing or
extra, or any verdict differs from the expected one.

### Look at a run

| File | Content |
|---|---|
| `evidence/runs/<UTC>/<run>/expected.json` | the cell and the reference parents, written before the run |
| `.../driver.json` | bytes sent on both carriers, responses, the API's received headers, every exported span |
| `.../server.log`, `gateway.log`, `gateway-config.yaml` | the server's read-only tap lines; the gateway's log and configuration |
| `.../images.txt` | image IDs of every pod in the run |
| `results/runs/<UTC>.tsv` | one verdict row per run, call and subject |

`python3 lab/figures.py` draws span trees of selected published runs (`results/figures/`).

### Use an existing cluster

Skip `cluster-up`, set `KUBE_CONTEXT=<context>` and `REGISTRY=<registry/prefix>` the cluster can pull from:
`scripts/lab.sh build` and `install` push the three server images there.

### Change a version

Pins are in `scripts/versions.env` (tools, base images by digest, Collector, agentgateway) and in the server
lockfiles (`lab/servers/ts/package-lock.json`, `lab/servers/py/requirements-v*.lock.txt`). `scripts/check-versions.sh`
lists newer upstream releases. A newer MCP SDK or OpenTelemetry package can change the measured behaviour: run the
plan again and compare.

### Test in a throwaway VM

`scripts/multipass-e2e.sh` runs the quickstart in a fresh multipass VM from the committed tree (`UBUNTU=24.04|26.04`,
`RUNS`, `KEEP=1`), then reinstalls, runs one more round and uninstalls. Output: `evidence/e2e-multipass/<UTC>-ubuntu<ver>/`.

### Tests without a cluster

```sh
docker run --rm -v "$PWD":/src -w /src python:3.14-slim sh -c 'pip install -q pytest && pytest -q lab/tests'
```

They check that the published TSV is regenerated exactly from the evidence, that an independent implementation of
the verdict rules (`lab/verdict_independent.py`, walks the span graph instead) agrees except on five known rows, that
the plan covers every published cell-mode, and that `summary.py` accepts the published runs and rejects a changed
outcome, a duplicate row, an unlisted setup failure and an unplanned run. The expected outcomes are the published
ones: a check of consistency over time, not an independent proof that a verdict is right; that is the role of
`lab/verdict_independent.py`.

### Versions

| Component | Version |
|---|---|
| k3d / kubectl / helm | v5.9.0 / v1.37.1 / v4.3.0 |
| k3s | v1.37.1-k3s1 |
| Node (TS server) | 24.21.0 (published runs: 22) |
| CPython (Python servers, driver, API) | 3.14.8 (published runs: 3.12) |
| OpenTelemetry Collector contrib | 0.162.0 (published runs: 0.151.0) |
| agentgateway | v1.6.0 |
| MCP TypeScript SDK | 1.32.1 |
| MCP Python SDK | 2.3.0 (`py2`), 1.30.0 (`py1`, kept on purpose) |
| OpenTelemetry JS | `sdk-node` 0.223.0, `auto-instrumentations-node` 0.81.0, `api` 1.9.1 (published runs: 1.9.0) |
| OpenTelemetry Python | `opentelemetry-sdk` 1.45.1, instrumentation 0.66b1 |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `docker not usable` right after `platform.sh install` | the new `docker` group applies to new shells; the scripts rerun themselves under `sg docker`, otherwise log in again |
| `image import incomplete` | the image tarball did not reach every k3d node; rerun `scripts/lab.sh install` |
| a run with `77` and `server_O': False` | the server's outbound span was not exported within 15 s; tolerated once for the first TS P6 run (fresh pod), a failure anywhere else |
| `summary` lists `runner failed` | see `lab/.runtime/runner.log` for that entry's output |

## Repository layout

```
lab/
  servers/ts, servers/py   MCP servers (one tool each) + the read-only tap and the Y2/Y2b hooks (oa-hooks.mjs, oa_hooks.py)
  driver/ downstream/ collector/ common/   driver, downstream API, span reader (stdlib Python)
  k8s/                     Helm chart: Collector, downstream API, driver
  plan.py run_cell.py      the test plan and the runner of one cell-mode
  expected.py verdict.py   reference parents (before a run) and verdicts (after)
  expected-outcomes.tsv    the published outcome per plan entry (make_expected.py)
  verdict_independent.py audit_*.py compare_tap.py repro_baggage.py figures.py summarize.py   checks and views
  tests/                   regression tests (no cluster)
scripts/                   platform.sh, lab.sh, summary.py, versions.env, check-versions.sh, multipass-e2e.sh
evidence/measured/         the published runs (c1, c2)
evidence/e2e-multipass/    reproduction runs in fresh VMs
results/measurements.tsv   verdicts of the published runs
docs/                      project page (GitHub Pages: index.html, span-tree figures)
```

## License

Apache License 2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE).
