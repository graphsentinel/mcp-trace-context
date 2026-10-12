#!/usr/bin/env bash
# Builds the gh-pages branch from two branches' docs/ folders, for GitHub Pages ("Deploy from a branch": gh-pages, /):
#   /                    ← main:docs                (the page of the main branch)
#   /observability-day/  ← observability-day:docs   (the page of this branch)
# Run from any branch; commits to the local gh-pages branch only (no push). Re-run after either docs/ changes.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd); cd "$ROOT"
BRANCHES=(main observability-day)
for b in "${BRANCHES[@]}"; do git rev-parse --verify -q "$b" >/dev/null || { echo "branch $b not found" >&2; exit 1; }; done
W=$(mktemp -d); trap 'git worktree remove --force "$W" >/dev/null 2>&1 || true; rm -rf "$W"' EXIT
if git rev-parse --verify -q gh-pages >/dev/null; then
  git worktree add -q "$W" gh-pages
else
  git worktree add -q --detach "$W" && git -C "$W" checkout -q --orphan gh-pages
fi
git -C "$W" rm -rqf --ignore-unmatch . >/dev/null; find "$W" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
git archive main docs | tar -x -C "$W" --strip-components=1
mkdir -p "$W/observability-day"
git archive observability-day docs | tar -x -C "$W/observability-day" --strip-components=1
touch "$W/.nojekyll"
git -C "$W" add -A
if git -C "$W" diff --cached --quiet; then echo "gh-pages unchanged"; exit 0; fi
git -C "$W" commit -qm "Pages: / = main:docs ($(git rev-parse --short main)), /observability-day/ = observability-day:docs ($(git rev-parse --short observability-day))"
echo "gh-pages: $(git rev-parse --short gh-pages)"; git -C "$W" ls-files | sed 's/^/  /'
