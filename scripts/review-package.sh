#!/usr/bin/env bash
# Write a review package: commit list, stat summary and the net diff with extended context.
# The diff leaves out uv.lock so a review reads small. The stat still lists it in one line.
# The stat and the diff use the three-dot range, so they start at the merge base like the commit
# list does: work that landed on BASE after HEAD branched is not shown as a deletion.
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

# Resolve OUTFILE against the caller's directory, then run from the repo root. A pathspec of
# `.` and the exclude both depend on the current directory, so a call from a subdirectory
# would otherwise drop files from the diff while the stat still listed them.
case "$out" in
  /*) ;;
  *) out="$PWD/$out" ;;
esac
cd "$(git rev-parse --show-toplevel)"

git rev-parse --verify --quiet "$base" >/dev/null || { echo "bad BASE: $base" >&2; exit 2; }
git rev-parse --verify --quiet "$head" >/dev/null || { echo "bad HEAD: $head" >&2; exit 2; }

# With more than one merge base (each side merged the other), the three-dot stat and diff start
# from whichever base git picks and can show work BASE already has. Git warns on stderr only, so
# the package carries the warning where a reviewer reads it.
bases=$(git merge-base --all "$base" "$head" | wc -l | tr -d ' ')

{
  echo "# Review package: ${base}..${head}"
  echo
  if [ "$bases" -gt 1 ]; then
    echo "> Warning: ${base} and ${head} have ${bases} merge bases. The stat and diff below start from"
    echo "> one of them and may show changes ${base} already has; trust the commit list."
    echo
  fi
  echo "## Commits"
  git log --oneline "${base}..${head}"
  echo
  echo "## Files changed"
  git diff --stat "${base}...${head}"
  echo
  echo "## Diff"
  git diff -U10 "${base}...${head}" -- . ':(exclude)uv.lock'
} > "$out"

commits=$(git rev-list --count "${base}..${head}")
echo "wrote ${out}: ${commits} commit(s), $(wc -c < "$out" | tr -d ' ') bytes"
if [ "$bases" -gt 1 ]; then
  echo "warning: ${bases} merge bases between ${base} and ${head}; the diff may include work ${base} already has" >&2
fi
