# OBI inside k3d nodes: propagation disabled (supporting evidence for Y3)

Two fresh multipass VMs (kernel lockdown `none`), lab installed on **k3d** exactly as in the quickstart, OBI v0.14.0 from
`lab/k8s/obi/values.yaml`, the six Y3 cells run once each (5 runs per cell). In both, OBI logged at startup that its
FIONREAD compensation could not attach and that **context propagation is disabled**; no run propagated a traceparent
to the downstream API (subject D: `ABSENT` in every completed run).

| File | What |
|---|---|
| `env-ubuntu24.04.txt`, `env-ubuntu26.04.txt` | kernel (6.8.0-142, 7.0.0-34), lockdown, distribution |
| `obi-ubuntu24.04-6.8.0-142.log`, `obi-ubuntu26.04-7.0.0-34.log` | OBI DaemonSet logs (`kubectl logs`), incl. "context propagation is disabled" |
| `c5-ubuntu24.04-verdicts.tsv`, `c5-ubuntu26.04-verdicts.tsv` | `lab/verdict.py` rows of those runs |

These runs are **not** part of `results/measurements.tsv`. The published Y3 results (`evidence/measured/c5-*`) come from a
native single-node k3s in a third VM, where OBI logged "FIONREAD compensation verified" (`evidence/measured/c5-obi.log`).
Related upstream: open-telemetry/opentelemetry-ebpf-instrumentation PR #3076 and issue #3284.
