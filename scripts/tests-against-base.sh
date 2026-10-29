#!/usr/bin/env bash
# Run HEAD's tests against BASE's source, so a test that claims to prove a fix can show it: it must
# fail here. A test that passes here passes against the old code too: a drift guard, coverage, or a
# test that checks nothing. A collection error means the test imports something BASE lacks, which
# is a fail as well.
#
# BASE's tree is unpacked into a temporary directory, HEAD's test directories replace BASE's, and
# pytest runs there with PYTHONPATH on BASE's src/ directories, ahead of the editable installs in
# the workspace venv. Subprocesses the tests start inherit PYTHONPATH, so they run BASE's code too.
#
# The interpreter and the dependencies are the checkout's (`uv run python`), so run this from a
# checkout at HEAD; the script warns when the checkout is elsewhere. A package HEAD has whose src/
# is missing in BASE would import from the editable install, that is, from the checkout, and its
# tests would pass for the wrong reason: the script warns and leaves that package's tests out.
#
# Usage: scripts/tests-against-base.sh BASE HEAD [pytest options...] [test paths...]
#   scripts/tests-against-base.sh main feature -k "relative_spec or port_in_use"
#   scripts/tests-against-base.sh main feature packages/mcp-gateway/tests/test_cli.py
# Without a test path every test directory of HEAD runs; with one, only what is named.
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: scripts/tests-against-base.sh BASE HEAD [pytest options...] [test paths...]" >&2
  exit 2
fi
base=$1
head=$2
shift 2

cd "$(git rev-parse --show-toplevel)"
git rev-parse --verify --quiet "${base}^{commit}" >/dev/null || { echo "bad BASE: $base" >&2; exit 2; }
git rev-parse --verify --quiet "${head}^{commit}" >/dev/null || { echo "bad HEAD: $head" >&2; exit 2; }
if [ "$(git rev-parse "${head}^{commit}")" != "$(git rev-parse HEAD)" ]; then
  echo "warning: the checkout is not at ${head}; the interpreter and dependencies are the checkout's" >&2
fi

python=$(uv run python -c 'import sys; print(sys.executable)')
work=$(mktemp -d "${TMPDIR:-/tmp}/tests-against-base.XXXXXX")
trap 'rm -rf "$work"' EXIT

git archive "$base" | tar -x -C "$work"

# HEAD's test directories, read from HEAD's tree: the root tests/ and each package's tests/.
# A package without src/ in BASE is left out, since its tests would run the checkout's code.
tests=()
if git rev-parse --verify --quiet "${head}:tests" >/dev/null; then
  tests+=(tests)
fi
while IFS= read -r pkg; do
  [ -n "$pkg" ] || continue
  git rev-parse --verify --quiet "${head}:${pkg}/tests" >/dev/null || continue
  if git rev-parse --verify --quiet "${base}:${pkg}/src" >/dev/null; then
    tests+=("${pkg}/tests")
  else
    echo "warning: ${pkg} has no src/ in ${base}; its tests are left out" >&2
  fi
done < <(git ls-tree -d --name-only "$head" -- packages/)
if [ ${#tests[@]} -eq 0 ]; then
  echo "no test directories of ${head} can run against ${base}" >&2
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

# An argument that names a file or directory in the tree (with or without ::node) selects the
# tests to run; otherwise every test directory runs.
targets=("${tests[@]}")
for arg in "$@"; do
  if [ -e "${work}/${arg%%::*}" ]; then
    targets=()
    break
  fi
done

echo "tests of ${head} against the source of ${base}" >&2
cd "$work"
PYTHONPATH="${pythonpath}${PYTHONPATH:+:${PYTHONPATH}}" "$python" -X dev -m pytest \
  -p no:cacheprovider -rA --no-header --continue-on-collection-errors "$@" ${targets[@]+"${targets[@]}"}
