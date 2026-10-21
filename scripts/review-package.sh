#!/usr/bin/env bash
# Write a review package: commit list, stat summary and the net diff with extended context.
# The diff leaves out uv.lock so a review reads small. The stat still lists it in one line.
#
# Usage: scripts/review-package.sh BASE HEAD OUTFILE
set -euo pipefail

if [ $# -ne 3 ]; then
  echo "usage: scripts/review-package.sh BASE HEAD OUTFILE" >&2
  exit 2
fi

base=$1
head=$2
out=$3

git rev-parse --verify --quiet "$base" >/dev/null || { echo "bad BASE: $base" >&2; exit 2; }
git rev-parse --verify --quiet "$head" >/dev/null || { echo "bad HEAD: $head" >&2; exit 2; }

{
  echo "# Review package: ${base}..${head}"
  echo
  echo "## Commits"
  git log --oneline "${base}..${head}"
  echo
  echo "## Files changed"
  git diff --stat "${base}..${head}"
  echo
  echo "## Diff"
  git diff -U10 "${base}..${head}" -- . ':(exclude)uv.lock'
} > "$out"

commits=$(git rev-list --count "${base}..${head}")
echo "wrote ${out}: ${commits} commit(s), $(wc -c < "$out" | tr -d ' ') bytes"
