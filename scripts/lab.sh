#!/usr/bin/env bash
# Layer 2, the lab: our images, the Helm chart (lab/k8s) and the tests, on top of the platform (scripts/platform.sh).
#
#   scripts/lab.sh build       build the three MCP server images (TypeScript SDK 1.32, Python SDK 1.30 and 2.3)
#   scripts/lab.sh install     load the images into the cluster, helm upgrade --install oa-lab, wait until ready
#   scripts/lab.sh uninstall   helm uninstall: removes everything the release created, including the MCP servers and
#                              gateways the tests started (the cluster and the platform stay)
#   scripts/lab.sh reinstall   build + uninstall + install: start again from zero after a code or config change
#   scripts/lab.sh test        run the whole test plan (lab/plan.py), compute verdicts, then summary
#   scripts/lab.sh summary     the last test checked against lab/expected-outcomes.tsv (exit 1 if any check fails)
#   scripts/lab.sh status      release, pods
#   scripts/lab.sh all         build + install + test
#
# Options (environment):
#   KUBE_CONTEXT=...  cluster to use (default k3d-oa-trace-context, created by scripts/platform.sh cluster-up)
#   REGISTRY=...      push the lab images to this registry instead of importing them into k3d
#                     (needed for any cluster that is not the lab's k3d cluster), e.g. registry.example.com/oa
#   RUNS=5            runs per main cell-mode (the published results use 5); controls keep their own counts
#   ENGINE=docker     container engine for builds (docker or podman); detected when unset
#
# Output of 'test': evidence/runs/<UTC>/ (one directory per run) and results/runs/<UTC>.tsv (verdicts), both
# git-ignored; the published runs are in evidence/measured/ and results/measurements.tsv.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=versions.env
source "$ROOT/scripts/versions.env"
RUNS=${RUNS:-5}; REGISTRY=${REGISTRY:-}
export KUBE_CONTEXT=${KUBE_CONTEXT:-k3d-$CLUSTER}
K="kubectl --context $KUBE_CONTEXT"
H="helm --kube-context $KUBE_CONTEXT"
RELEASE=oa-lab; RELEASE_NS=oa-lab-release; CHART="$ROOT/lab/k8s"
STATE="$ROOT/lab/.runtime"; mkdir -p "$STATE"

log() { printf '\n==> %s\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "$1 not found: run scripts/platform.sh install"; }

# right after 'platform.sh install' the docker group is not active in this shell yet: run again under it
if [ -z "${ENGINE:-}" ] && command -v docker >/dev/null && ! docker info >/dev/null 2>&1 \
   && [ -z "${OA_SG:-}" ] && getent group docker | grep -qw "$(id -un)" && command -v sg >/dev/null; then
  export OA_SG=1; exec sg docker -c "$(printf '%q ' "$0" "$@")"
fi
if [ -z "${ENGINE:-}" ]; then
  if command -v docker >/dev/null && docker info >/dev/null 2>&1; then ENGINE=docker
  elif command -v podman >/dev/null; then ENGINE=podman
  else die "no usable container engine: scripts/platform.sh install, then log in again (docker group)"; fi
fi
# k3d's nodes run in the runtime k3d talks to (docker CLI / DOCKER_HOST), whatever engine builds the images
NODE_RT=docker; command -v docker >/dev/null || NODE_RT=podman
case "$(uname -m)" in x86_64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; *) die "unsupported architecture $(uname -m)" ;; esac

LAB_IMAGES=("$TS1_IMAGE" "$PY1_IMAGE" "$PY2_IMAGE")
# the Python image runs the driver, downstream API and span reader: pulled by digest, used by its tag
PYTHON_RUNTIME=${PYTHON_IMAGE%@*}
THIRD_PARTY=("$PYTHON_RUNTIME" "$COLLECTOR_IMAGE" "$AGW_IMAGE")
have_image() { $ENGINE image inspect "$1" >/dev/null 2>&1; }
# reference the cluster pulls: the local tag (k3d import) or the registry copy
ref() { if [ -n "$REGISTRY" ] && [[ "$1" == localhost/* ]]; then echo "$REGISTRY/${1#localhost/}"; else echo "$1"; fi; }
on_lab_k3d() { [ "$KUBE_CONTEXT" = "k3d-$CLUSTER" ] && [ -z "$REGISTRY" ]; }
# pull <repo:tag@sha256:...>: by digest, then tag it, so that the tag names exactly the pinned content
pull_pinned() {
  local img=$1 repo=${1%%@*} digest=${1##*@}
  $ENGINE pull -q --platform "linux/$ARCH" "${repo%:*}@$digest" >/dev/null
  $ENGINE tag "${repo%:*}@$digest" "$repo"
}

# ---------------------------------------------------------------- build
cmd_build() {
  log "pull base images (by digest)"
  pull_pinned "$NODE_IMAGE"; pull_pinned "$PYTHON_IMAGE"
  log "TypeScript server, MCP TypeScript SDK 1.32 ($TS1_IMAGE)"
  $ENGINE build -q --build-arg BASE="$NODE_IMAGE" -t "$TS1_IMAGE" -f "$ROOT/lab/servers/ts/Containerfile" "$ROOT/lab/servers/ts" >/dev/null
  log "Python server, MCP Python SDK 1.30 ($PY1_IMAGE)"
  $ENGINE build -q --build-arg BASE="$PYTHON_IMAGE" -t "$PY1_IMAGE" -f "$ROOT/lab/servers/py/Containerfile.v1" "$ROOT/lab/servers/py" >/dev/null
  log "Python server, MCP Python SDK 2.3 ($PY2_IMAGE)"
  $ENGINE build -q --build-arg BASE="$PYTHON_IMAGE" -t "$PY2_IMAGE" -f "$ROOT/lab/servers/py/Containerfile.v2" "$ROOT/lab/servers/py" >/dev/null
  log "built images"
  $ENGINE images --format '{{.Repository}}:{{.Tag}}' | grep -E '/oa-(ts1|py1|py2):' | sort
}

# ---------------------------------------------------------------- install
# k3d: images go into the nodes as one single-platform tarball. Docker 29 uses the containerd image store, where
# 'docker save' of a multi-platform image writes an index whose other-platform blobs are missing; k3s's
# 'ctr import' then fails while k3d still reports success. Hence --platform, and the check afterwards.
import_k3d() {
  local tar="$STATE/images.tar"
  if [ "$ENGINE" = podman ]; then podman save -m -o "$tar" "$@"
  # --platform needs Docker API 1.48+ (Docker 28); older engines use the classic image store, where plain save works
  elif ! docker save --platform "linux/$ARCH" -o "$tar" "$@" 2>/dev/null; then docker save -o "$tar" "$@"; fi
  k3d image import -c "$CLUSTER" "$tar"
  rm -f "$tar"
  local node img have missing=0
  for node in $(k3d node list -o json | python3 -c 'import sys,json;[print(n["name"]) for n in json.load(sys.stdin) if n["role"] in ("server","agent") and n["runtimeLabels"].get("k3d.cluster")=="'"$CLUSTER"'"]'); do
    have=$($NODE_RT exec "$node" crictl images -o json | python3 -c 'import sys,json;[print(t) for i in json.load(sys.stdin)["images"] for t in i["repoTags"]]')
    for img in "$@"; do grep -qx "$img" <<<"$have" || { echo "missing in $node: $img" >&2; missing=1; }; done
  done
  [ "$missing" = 0 ] || die "image import incomplete (see above)"
  echo "all $# images present on every node"
}

load_images() {
  for i in "${LAB_IMAGES[@]}"; do have_image "$i" || die "$i missing: run scripts/lab.sh build"; done
  if on_lab_k3d; then
    log "images: pull third-party, import all into k3d"
    pull_pinned "$PYTHON_IMAGE"
    for i in "$COLLECTOR_IMAGE" "$AGW_IMAGE"; do $ENGINE pull -q --platform "linux/$ARCH" "$i" >/dev/null; done
    import_k3d "${THIRD_PARTY[@]}" "${LAB_IMAGES[@]}"
  elif [ -n "$REGISTRY" ]; then
    log "images: push lab images to $REGISTRY (third-party images are pulled by the cluster)"
    for i in "${LAB_IMAGES[@]}"; do $ENGINE tag "$i" "$(ref "$i")" && $ENGINE push -q "$(ref "$i")" >/dev/null && echo "  $(ref "$i")"; done
  else
    die "context $KUBE_CONTEXT is not the lab's k3d cluster: set REGISTRY=<registry/prefix> the cluster can pull from"
  fi
}

pull_policy() { if on_lab_k3d; then echo Never; else echo IfNotPresent; fi; }

cmd_install() {
  need kubectl; need helm
  $K get --raw /readyz >/dev/null 2>&1 || die "cluster $KUBE_CONTEXT not reachable: scripts/platform.sh cluster-up (or set KUBE_CONTEXT)"
  local skew
  skew=$($K version -o json 2>/dev/null | python3 -c 'import sys,json;v=json.load(sys.stdin);c,s=int(v["clientVersion"]["minor"]),int(v["serverVersion"]["minor"].rstrip("+"));print(f"{c} {s}" if abs(c-s)>1 else "")' 2>/dev/null || true)
  if [ -n "$skew" ]; then echo "warning: kubectl 1.${skew% *} vs cluster 1.${skew#* } is outside the supported skew (±1); UPGRADE_TOOLS=1 scripts/platform.sh install" >&2; fi
  load_images
  log "helm upgrade --install $RELEASE (context $KUBE_CONTEXT)"
  # the release namespace holds only Helm's records; created here (and labelled) so that uninstall can tell it is ours
  if ! $K get ns "$RELEASE_NS" >/dev/null 2>&1; then
    $K create ns "$RELEASE_NS" >/dev/null && $K label ns "$RELEASE_NS" oa/release-namespace=true >/dev/null
  fi
  $H upgrade --install "$RELEASE" "$CHART" -n "$RELEASE_NS" --wait --timeout 10m \
    --set imagePullPolicy="$(pull_policy)" \
    --set images.python="$PYTHON_RUNTIME" --set images.collector="$COLLECTOR_IMAGE" \
    --set-file code.otlp="$ROOT/lab/common/otlp.py" --set-file code.driver="$ROOT/lab/driver/driver.py" \
    --set-file code.downstream="$ROOT/lab/downstream/downstream.py" --set-file code.spanReader="$ROOT/lab/collector/span_reader.py"
  $K -n oa-lab wait --for=condition=Ready pod/driver --timeout=180s >/dev/null
  log "collector and downstream answer"
  for _ in $(seq 1 30); do
    if $K -n oa-lab exec driver -- python3 -c "import urllib.request as u;u.urlopen('http://downstream-api.oa-lab:8080/healthz',timeout=5);u.urlopen('http://otel-collector.oa-obs:8081/since/0',timeout=5)" 2>/dev/null; then
      echo "ready"; return 0; fi
    sleep 2
  done
  die "collector or downstream API not reachable from the driver"
}

cmd_uninstall() {
  need kubectl; need helm
  if $H status "$RELEASE" -n "$RELEASE_NS" >/dev/null 2>&1; then
    log "helm uninstall $RELEASE (removes its namespaces oa-lab and oa-obs, with every server and gateway the tests started)"
    $H uninstall "$RELEASE" -n "$RELEASE_NS" --wait --timeout 10m
  else
    echo "release $RELEASE not installed in $KUBE_CONTEXT"
  fi
  # The release namespace is removed only if this script created it (label oa/release-namespace) and nothing but
  # Helm's records and the namespace defaults is left in it, checked over every namespaced resource type; if the
  # check itself fails, the namespace is kept.
  if [ "$($K get ns "$RELEASE_NS" -o jsonpath='{.metadata.labels.oa/release-namespace}' 2>/dev/null)" = "true" ]; then
    local types other
    if types=$($K api-resources --namespaced --verbs=list -o name 2>/dev/null | grep -v '^events' | paste -sd, -) \
       && other=$($K -n "$RELEASE_NS" get "$types" -o name 2>/dev/null); then
      other=$(grep -v -E "^(configmap/kube-root-ca\.crt|serviceaccount/default|secret/sh\.helm\.release\.v1\.$RELEASE\.v[0-9]+)\$" <<<"$other" || true)
      if [ -z "$other" ]; then $K delete ns "$RELEASE_NS" --wait=true --timeout=120s
      else echo "namespace $RELEASE_NS kept, it holds: $(tr '\n' ' ' <<<"$other")"; fi
    else
      echo "namespace $RELEASE_NS kept: could not list its contents"
    fi
  fi
  # check: none of the chart's namespaces is left
  local left
  left=$($K get ns oa-lab oa-obs -o name --ignore-not-found) || die "could not verify the removal (API error)"
  [ -z "$left" ] || die "still present after uninstall: $(tr '\n' ' ' <<<"$left")"
  echo "removed; the cluster is still there (scripts/platform.sh cluster-down deletes it)"
}

cmd_status() {
  $H status "$RELEASE" -n "$RELEASE_NS" 2>/dev/null | sed -n '1,6p' || echo "release $RELEASE not installed"
  for ns in oa-obs oa-lab; do $K -n "$ns" get pods --no-headers 2>/dev/null | sed "s/^/$ns  /"; done
}

# ---------------------------------------------------------------- test
cmd_test() {
  need kubectl; need helm
  $H status "$RELEASE" -n "$RELEASE_NS" >/dev/null 2>&1 || die "lab not installed: scripts/lab.sh install"
  local stamp; stamp=$(date -u +%Y%m%dT%H%M%SZ)
  local ev="$ROOT/evidence/runs/$stamp" tsv="$ROOT/results/runs/$stamp.tsv"
  echo "$tsv" > "$STATE/last-tsv"
  export OA_IMAGE_TS1 OA_IMAGE_PY1 OA_IMAGE_PY2 OA_IMAGE_AGW OA_PULL_POLICY
  OA_IMAGE_TS1=$(ref "$TS1_IMAGE"); OA_IMAGE_PY1=$(ref "$PY1_IMAGE"); OA_IMAGE_PY2=$(ref "$PY2_IMAGE")
  OA_IMAGE_AGW=$AGW_IMAGE; OA_PULL_POLICY=$(pull_policy)
  log "test plan: $(python3 "$ROOT/lab/plan.py" list --runs "$RUNS" | tail -1) (RUNS=$RUNS); evidence: evidence/runs/$stamp"
  # plan.py writes the plan before the first run (complete=false) and records every runner's exit code
  python3 "$ROOT/lab/plan.py" run --runs "$RUNS" --evidence "$ev" --state "$STATE"
  log "verdicts"
  python3 "$ROOT/lab/verdict.py" --evidence "$ev" --out "$tsv"
  cmd_summary
}

cmd_summary() {
  [ -f "$STATE/test-plan.json" ] || die "no test run recorded: run scripts/lab.sh test"
  [ -f "$STATE/last-tsv" ] || die "no verdicts recorded: run scripts/lab.sh test"
  local tsv; tsv=$(cat "$STATE/last-tsv")
  [ -f "$tsv" ] || die "verdict file $tsv missing (the test did not get that far): run scripts/lab.sh test"
  python3 "$ROOT/scripts/summary.py" --plan "$STATE/test-plan.json" --runner-failures "$STATE/runner-failures" \
    --expected "$ROOT/lab/expected-outcomes.tsv" --markdown "$STATE/summary.md" "$tsv"
}

case "${1:-}" in
  build) cmd_build ;;
  install) cmd_install ;;
  uninstall) cmd_uninstall ;;
  reinstall) cmd_build; cmd_uninstall; cmd_install ;;
  test) cmd_test ;;
  summary) cmd_summary ;;
  status) cmd_status ;;
  all) cmd_build; cmd_install; cmd_test ;;
  *) sed -n '2,23p' "$0"; exit 2 ;;
esac
