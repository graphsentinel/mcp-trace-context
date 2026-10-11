#!/usr/bin/env bash
# The two-context test against an MCP server in any cluster: runs lab/probe/probe.py once in a throwaway pod.
#   scripts/probe.sh -n <namespace> -- --url http://<server>.<ns>:<port>/mcp --tool <name> --args '<json>' [--echo 9000 | --received <url>]
# Options (environment): KUBE_CONTEXT (current context), IMAGE (the lab's Python image), see lab/probe/probe.py --help.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
. "$ROOT/scripts/versions.env"
NS=default
while [ $# -gt 0 ]; do case "$1" in -n) NS=$2; shift 2 ;; --) shift; break ;; *) break ;; esac; done
IMAGE=${IMAGE:-${PYTHON_IMAGE%@*}}
K=(kubectl); [ -n "${KUBE_CONTEXT:-}" ] && K+=(--context "$KUBE_CONTEXT")
# the script goes in on stdin: nothing to build or mount
exec "${K[@]}" -n "$NS" run "oa-probe-$(date +%s)" --rm -i --restart=Never --quiet --image="$IMAGE" \
  --image-pull-policy=IfNotPresent -- python3 - "$@" < "$ROOT/lab/probe/probe.py"
