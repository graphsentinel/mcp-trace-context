#!/usr/bin/env bash
# Compares the pins in scripts/versions.env and the server lockfiles with the latest upstream stable releases.
# Read-only: it changes nothing. Update the pins by hand, then run scripts/lab.sh all to re-measure.
# A newer MCP SDK or OpenTelemetry package can change the measured behaviour: measure it as a separate run.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=versions.env
source "$ROOT/scripts/versions.env"

gh_latest() { curl -fsSL "https://api.github.com/repos/$1/releases/latest" | python3 -c 'import sys,json;print(json.load(sys.stdin)["tag_name"])' 2>/dev/null || echo "?"; }
npm_latest() { curl -fsSL "https://registry.npmjs.org/$1/latest" | python3 -c 'import sys,json;print(json.load(sys.stdin)["version"])' 2>/dev/null || echo "?"; }
pypi_latest() { curl -fsSL "https://pypi.org/pypi/$1/json" | python3 -c 'import sys,json;print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null || echo "?"; }
node_lts() { curl -fsSL https://nodejs.org/dist/index.json | python3 -c 'import sys,json;print([x["version"] for x in json.load(sys.stdin) if x["lts"]][0].lstrip("v"))' 2>/dev/null || echo "?"; }
python_latest() {
  curl -fsSL "https://hub.docker.com/v2/repositories/library/python/tags?page_size=100&name=-slim-trixie" \
    | python3 -c 'import sys,json,re
v=[t["name"].split("-")[0] for t in json.load(sys.stdin)["results"] if re.fullmatch(r"3\.\d+\.\d+-slim-trixie",t["name"])]
print(max(v,key=lambda s:tuple(map(int,s.split("."))))) if v else print("?")' 2>/dev/null || echo "?"
}
npm_pinned() { python3 -c 'import sys,json;print(json.load(open(sys.argv[1]))["packages"]["node_modules/"+sys.argv[2]]["version"])' "$ROOT/lab/servers/ts/package-lock.json" "$1"; }
pip_pinned() { sed -n "s/^$1==//p" "$ROOT/lab/servers/py/requirements-v$2.lock.txt"; }

row() { local s="same"; [ "$2" != "$3" ] && s="NEWER AVAILABLE"; [ "$3" = "?" ] && s="unknown"; printf '%-46s %-14s %-14s %s\n' "$1" "$2" "$3" "$s"; }
printf '%-46s %-14s %-14s %s\n' COMPONENT PINNED LATEST STATUS
row k3d                       "$K3D_VERSION"             "$(gh_latest k3d-io/k3d)"
row kubernetes                "$KUBECTL_VERSION"         "$(curl -fsSL https://dl.k8s.io/release/stable.txt)"
row k3s                       "${K3S_IMAGE##*:}"         "$(gh_latest k3s-io/k3s | tr + -)"
row helm                      "$HELM_VERSION"            "$(gh_latest helm/helm)"
row "node (LTS)"              "$(echo "${NODE_IMAGE%%@*}" | sed 's/.*://; s/-.*//')" "$(node_lts)"
row python                    "$(echo "${PYTHON_IMAGE%%@*}" | sed 's/.*://; s/-.*//')" "$(python_latest)"
row otel-collector-contrib    "${COLLECTOR_IMAGE##*:}"   "$(gh_latest open-telemetry/opentelemetry-collector-releases | tr -d v)"
row agentgateway              "${AGW_IMAGE##*:}"         "$(gh_latest agentgateway/agentgateway)"
for p in @modelcontextprotocol/sdk @opentelemetry/api @opentelemetry/sdk-node @opentelemetry/auto-instrumentations-node zod; do
  row "npm $p" "$(npm_pinned "$p")" "$(npm_latest "$p")"
done
row "pypi mcp (py2)" "$(pip_pinned mcp 2)" "$(pypi_latest mcp)"
printf '%-46s %-14s %-14s %s\n' "pypi mcp (py1)" "$(pip_pinned mcp 1)" "" "kept on purpose: the older SDK line is a measured subject"
for p in opentelemetry-sdk opentelemetry-distro opentelemetry-exporter-otlp-proto-http opentelemetry-instrumentation-httpx \
         opentelemetry-instrumentation-asgi opentelemetry-instrumentation-starlette httpx; do
  row "pypi $p" "$(pip_pinned "$p" 2)" "$(pypi_latest "$p")"
done
