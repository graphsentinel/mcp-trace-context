#!/usr/bin/env python3
"""Run one cell-mode (or control) N times in the lab cluster. Normally called by lab/plan.py (scripts/lab.sh test).

  lab/run_cell.py --config lab --Y Y0 --S ts1 --T http --I auto --M same [--variant main] [--runs 5] [--evidence DIR]

For each run: writes <DIR>/<run>/expected.json (expected graph, BEFORE the run), runs the driver in the cluster,
copies the driver record and the server's stderr (tap lines) into <DIR>/<run>/.
Cluster and images come from the environment (scripts/lab.sh sets them from scripts/versions.env):
KUBE_CONTEXT, OA_IMAGE_TS1, OA_IMAGE_PY1, OA_IMAGE_PY2, OA_IMAGE_AGW, OA_PULL_POLICY."""
import argparse, datetime, hashlib, json, os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lab"))
import expected  # noqa: E402

CTX = os.environ.get("KUBE_CONTEXT", "k3d-oa-trace-context")
NS = "oa-lab"
GW = ("Y1", "Y4")  # methods with agentgateway in front of the server
IMG = {"ts1": os.environ.get("OA_IMAGE_TS1", "localhost/oa-ts1:1.32.1"),
       "py1": os.environ.get("OA_IMAGE_PY1", "localhost/oa-py1:1.30.0"),
       "py2": os.environ.get("OA_IMAGE_PY2", "localhost/oa-py2:2.3.0")}
AGW = os.environ.get("OA_IMAGE_AGW", "ghcr.io/agentgateway/agentgateway:v1.6.0")
PULL = os.environ.get("OA_PULL_POLICY", "Never")
OTEL_ENV = {  # frozen for every run
    "OTEL_PROPAGATORS": "tracecontext,baggage", "OTEL_TRACES_SAMPLER": "parentbased_always_on",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector.oa-obs:4318", "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_TRACES_EXPORTER": "otlp", "OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none",
    "OTEL_BSP_SCHEDULE_DELAY": "200", "OTEL_NODE_RESOURCE_DETECTORS": "env,host,os",
}


def k(*args, check=True, capture=True, inp=None):
    r = subprocess.run(["kubectl", "--context", CTX, *args], text=True, capture_output=capture, input=inp, check=False)
    if check and r.returncode:
        raise SystemExit(f"kubectl {' '.join(args)}: {r.stderr}")
    return r.stdout


def launch(S, I, tap="1"):
    if S.startswith("ts"):
        # ESM app: OTel JS needs its loader hook for module patching (node:http); register alone patches only
        # diagnostics-channel instrumentations such as undici
        reg = ("--experimental-loader=@opentelemetry/instrumentation/hook.mjs "
               "--import @opentelemetry/auto-instrumentations-node/register ") if I == "auto" else ""
        hooks = "--import ./oa-hooks.mjs " if tap in ("1", "L0") else ""  # P6: tap=0 loads no hook module; L0 = loaded, silent
        return f"node {reg}{hooks}server.mjs"
    v = S[-1]
    entry = f"launch.py server_v{v}.py" if tap in ("1", "L0") else f"server_v{v}.py"
    return ("opentelemetry-instrument " if I == "auto" else "") + f"python3 {entry}"


def env_for(S, I, Y, T, tap="1"):
    e = {"OA_TAP": "1" if tap == "1" else "0", "OA_BRIDGE": {"Y2": "1", "Y2b": "activate"}.get(Y, "0"), "MCP_TRANSPORT": T,
         "OTEL_SERVICE_NAME": f"mcp-{S}", "DOWNSTREAM": "http://downstream-api.oa-lab:8080"}
    if I == "auto":
        e.update(OTEL_ENV)
    return e


def name_for(S, I, Y, tap="1"):
    return f"mcp-{S}-{I}-{Y.lower()}" + {"1": "", "0": "-notap", "L0": "-tapoff"}[tap]


def ensure_http_server(S, I, Y, tap="1"):
    name = name_for(S, I, Y, tap)
    env = [{"name": a, "value": b} for a, b in env_for(S, I, Y, "http", tap).items()]
    m = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": NS},
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                  "template": {"metadata": {"labels": {"app": name}},
                               "spec": {"containers": [{"name": "server", "image": IMG[S], "imagePullPolicy": PULL,
                                                        "command": ["sh", "-c", "exec " + launch(S, I, tap)], "env": env,
                                                        "ports": [{"containerPort": 8000}],
                                                        "readinessProbe": {"tcpSocket": {"port": 8000}, "periodSeconds": 1}}]}}}}
    svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": NS},
           "spec": {"selector": {"app": name}, "ports": [{"port": 8000}]}}
    k("apply", "-f", "-", inp=json.dumps({"apiVersion": "v1", "kind": "List", "items": [m, svc]}))
    k("-n", NS, "rollout", "status", f"deploy/{name}", "--timeout=120s")
    return name


Y4_POLICY = (  # Y4: copy the client's params._meta trace context into the headers before the MCP backend (route level)
    "      policies:\n        transformations:\n          request:\n            set:\n"
    + "".join(f"              {h}: 'coalesce(json(request.body).params._meta.{h}, request.headers[\"{h}\"])'\n"
              for h in ("traceparent", "tracestate", "baggage")))


def ensure_gateway(S, I, gw, tap="1", Y="Y1"):
    """agentgateway v1.6.0 as released in front of the server of the same method and tap mode; tracing on/off.
    Y1: as released. Y4: plus a route-level transformation policy that sets the trace headers from params._meta.
    (Before 2026-10-11 the upstream ignored the tap mode: the published c2 Y1 '-notap' runs reached the tapped server.)"""
    name = ("agw4" if Y == "Y4" else "agw") + f"-{S}-{I}-{gw}" + {"1": "", "0": "-notap", "L0": "-tapoff"}[tap]
    upstream = f"http://{name_for(S, I, Y, tap)}.{NS}:8000/mcp"
    cfg = ""
    if gw == "on":
        # syntax of agentgateway's own examples/mcp-telemetry/config.yaml (v1.6.0); `config.tracing` is deprecated
        cfg = ("frontendPolicies:\n  tracing:\n    host: otel-collector.oa-obs:4317\n    randomSampling: true\n")
    cfg += ("binds:\n- port: 8080\n  listeners:\n  - routes:\n    - matches:\n      - path: {exact: /mcp}\n"
            + (Y4_POLICY if Y == "Y4" else "")
            + f"      backends:\n      - mcp:\n          targets:\n          - name: oa\n            mcp:\n              host: {upstream}\n")
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": NS}, "data": {"config.yaml": cfg}}
    dep = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": NS},
           "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                    "template": {"metadata": {"labels": {"app": name}, "annotations": {"oa/config-hash": hashlib.sha256(cfg.encode()).hexdigest()[:12]}},
                                 "spec": {"containers": [{"name": "agw", "image": AGW,
                                                          "imagePullPolicy": PULL, "args": ["-f", "/config/config.yaml"],
                                                          "ports": [{"containerPort": 8080}],
                                                          "readinessProbe": {"tcpSocket": {"port": 8080}, "periodSeconds": 1},
                                                          "volumeMounts": [{"name": "c", "mountPath": "/config"}]}],
                                          "volumes": [{"name": "c", "configMap": {"name": name}}]}}}}
    svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": NS},
           "spec": {"selector": {"app": name}, "ports": [{"port": 8080}]}}
    k("apply", "-f", "-", inp=json.dumps({"apiVersion": "v1", "kind": "List", "items": [cm, dep, svc]}))
    k("-n", NS, "rollout", "status", f"deploy/{name}", "--timeout=120s")
    return name, cfg


def ensure_stdio_pod(S):
    name = f"stdio-{S}"
    if k("-n", NS, "get", "pod", name, "--ignore-not-found", "-o", "name").strip():
        return name
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NS},
           "spec": {"containers": [{"name": "s", "image": IMG[S], "imagePullPolicy": PULL,
                                    "command": ["sleep", "infinity"],
                                    "volumeMounts": [{"name": "common", "mountPath": "/lab/common"},
                                                     {"name": "driver", "mountPath": "/lab/driver"}]}],
                    "volumes": [{"name": "common", "configMap": {"name": "lab-common"}},
                                {"name": "driver", "configMap": {"name": "driver-code"}}]}}
    k("apply", "-f", "-", inp=json.dumps(pod))
    k("-n", NS, "wait", "--for=condition=Ready", f"pod/{name}", "--timeout=120s")
    return name


def main():
    ap = argparse.ArgumentParser()
    for a in ("config", "Y", "S", "T", "I"):
        ap.add_argument(f"--{a}", required=True)
    ap.add_argument("--M", default="same")
    ap.add_argument("--variant", default="main")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--tap", default="1")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--gw", default="on", choices=["on", "off"])
    ap.add_argument("--evidence", default=os.path.join(ROOT, "evidence", "runs", "manual"))
    a = ap.parse_args()

    cell = {"Y": a.Y, "S": a.S, "T": a.T, "I": a.I, "M": a.M, "variant": a.variant, "config": a.config, "tap": a.tap}
    svc = f"mcp-{a.S}"
    gwname = gwcfg = None
    if a.Y in GW:
        cell["gw_tracing"] = a.gw
    if a.T == "http":
        name = ensure_http_server(a.S, a.I, a.Y, a.tap)
        if a.Y in GW:
            gwname, gwcfg = ensure_gateway(a.S, a.I, a.gw, a.tap, a.Y)
    else:
        name = ensure_stdio_pod(a.S)
    for r in range(a.start, a.start + a.runs):
        run = f"{a.config}-{a.Y}" + (f"gw{a.gw}" if a.Y in GW else "") + f"-{a.S}-{a.T}-{a.I}-{a.M}-{a.variant}" \
            + {"1": "", "0": "-notap", "L0": "-tapoff"}[a.tap] + f"-r{r}"
        ev = os.path.join(a.evidence, run)
        os.makedirs(ev, exist_ok=True)
        with open(os.path.join(ev, "expected.json"), "w") as f:  # pre-registered, before the run
            json.dump({"cell": cell, "graph": expected.graph(cell)}, f, indent=1)
        since = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        wait_o = ["--wait-o", "--server-service", svc] if a.I == "auto" else []
        if a.Y in GW and a.gw == "on":
            wait_o.append("--wait-gw")
        if a.T == "http":
            cmd = ["-n", NS, "exec", "driver", "--", "python3", "/lab/driver/driver.py", "--run", run, "--variant",
                   a.variant, "--mode", a.M, "--url",
                   f"http://{gwname}.{NS}:8080/mcp" if gwname else f"http://{name}.{NS}:8000/mcp", *wait_o]
        else:
            envs = " ".join(f"{x}={y}" for x, y in env_for(a.S, a.I, a.Y, "stdio", a.tap).items())
            cmd = ["-n", NS, "exec", name, "--", "env", "OA_COMMON=/lab/common", "python3", "/lab/driver/driver.py",
                   "--run", run, "--variant", a.variant, "--mode", "same",
                   "--stdio", f"cd /app && env {envs} {launch(a.S, a.I, a.tap)}", *wait_o]
        out = k(*cmd)
        pod = "driver" if a.T == "http" else name
        k("-n", NS, "cp", f"{pod}:/out/{run}.json", os.path.join(ev, "driver.json"))
        if a.T == "http":
            time.sleep(0.5)
            logs = k("-n", NS, "logs", f"deploy/{name}", f"--since-time={since}", check=False)
            open(os.path.join(ev, "server.log"), "w").write(logs)
            if gwname:
                open(os.path.join(ev, "gateway-config.yaml"), "w").write(gwcfg)
                open(os.path.join(ev, "gateway.log"), "w").write(
                    k("-n", NS, "logs", f"deploy/{gwname}", f"--since-time={since}", check=False))
        imgs = k("-n", NS, "get", "pods", "-o", "jsonpath={range .items[*]}{.metadata.name} {.status.containerStatuses[0].imageID}{\"\\n\"}{end}")
        open(os.path.join(ev, "images.txt"), "w").write(imgs)
        print(run, out.strip(), flush=True)


if __name__ == "__main__":
    main()
