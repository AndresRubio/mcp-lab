#!/usr/bin/env bash
# Run HEAD's tests against BASE's source, so a test that claims to prove a fix can show it: it must
# fail here. A test that passes here passes against the old code too: a drift guard, coverage, or a
# test that checks nothing. A collection error means the test imports something BASE lacks, which
# is a fail as well.
#
# BASE's tree is unpacked into a temporary directory, HEAD's test directories replace BASE's, and
# pytest runs there with PYTHONPATH on BASE's src/ directories, ahead of the editable install in the
# workspace venv. Subprocesses the tests start inherit PYTHONPATH, so they run BASE's code too.
# Dependencies are HEAD's (the workspace venv): a base that needs other ones fails to import.
#
# Usage: scripts/tests-against-base.sh BASE HEAD [pytest options...]
#   scripts/tests-against-base.sh main feature -k "relative_spec or port_in_use"
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: scripts/tests-against-base.sh BASE HEAD [pytest options...]" >&2
  exit 2
fi
base=$1
head=$2
shift 2

cd "$(git rev-parse --show-toplevel)"
git rev-parse --verify --quiet "${base}^{commit}" >/dev/null || { echo "bad BASE: $base" >&2; exit 2; }
git rev-parse --verify --quiet "${head}^{commit}" >/dev/null || { echo "bad HEAD: $head" >&2; exit 2; }

python=$(uv run python -c 'import sys; print(sys.executable)')
work=$(mktemp -d "${TMPDIR:-/tmp}/tests-against-base.XXXXXX")
trap 'rm -rf "$work"' EXIT

git archive "$base" | tar -x -C "$work"

# HEAD's test directories: the root tests/ and each package's tests/, exactly HEAD's set.
tests=()
for dir in tests packages/*/tests; do
  if git rev-parse --verify --quiet "${head}:${dir}" >/dev/null; then
    tests+=("$dir")
  fi
done
if [ ${#tests[@]} -eq 0 ]; then
  echo "no test directories in ${head}" >&2
  exit 2
fi
for dir in "${tests[@]}"; do
  rm -rf "${work:?}/${dir}"
done
git archive "$head" "${tests[@]}" | tar -x -C "$work"

pythonpath=""
for src in "$work"/packages/*/src; do
  pythonpath="${pythonpath:+${pythonpath}:}${src}"
done

echo "tests of ${head} against the source of ${base}" >&2
cd "$work"
PYTHONPATH="${pythonpath}${PYTHONPATH:+:${PYTHONPATH}}" "$python" -X dev -m pytest \
  -p no:cacheprovider -rA --no-header --continue-on-collection-errors "$@" "${tests[@]}"
