#!/usr/bin/env bash
# Run HEAD's tests against BASE's source, so a test that claims to prove a fix can show it: it must
# fail here. A test that passes here passes against the old code too: a drift guard, coverage, or a
# test that checks nothing. A collection error means the test imports something BASE lacks, which
# is a fail as well.
#
# BASE's tree is unpacked into a temporary directory, HEAD's test directories replace BASE's, and
# pytest runs there with PYTHONPATH on BASE's src/ directories, ahead of the editable installs in
# the workspace venv. Subprocesses the tests start inherit PYTHONPATH, so they run BASE's code too,
# and `uv run` inside a test is pointed at the checkout's venv without a sync, so it does not build
# a second one from BASE's lock. pytest runs under the flags scripts/check.sh uses, `-X dev` and
# `-X warn_default_encoding`, so a test fails here for the same warnings it fails for there.
#
# The interpreter and the dependencies are the checkout's (`uv run python`), so run this from a
# checkout at HEAD; the script warns when the checkout is elsewhere. A package is left out, with a
# warning, when BASE has no src/ for it or when HEAD's src/ has a top-level module or package BASE
# lacks: either way its tests would import the checkout's code through the editable install and
# pass for the wrong reason.
#
# Usage: scripts/tests-against-base.sh BASE HEAD [pytest options...] [test paths...]
#   scripts/tests-against-base.sh main feature -k "relative_spec or port_in_use"
#   scripts/tests-against-base.sh main feature packages/mcp-gateway/tests/test_cli.py
# Without a test path every test directory of HEAD runs; with one, only what is named. A test path
# is relative to the repository root, whatever the current directory; an absolute path works too.
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
venv=$(uv run python -c 'import sys; print(sys.prefix)')
work=$(mktemp -d "${TMPDIR:-/tmp}/tests-against-base.XXXXXX")
trap 'rm -rf "$work"' EXIT

git archive "$base" | tar -x -C "$work"

# HEAD's test directories, read from HEAD's tree: the root tests/ and each package's tests/.
tests=()
if git rev-parse --verify --quiet "${head}:tests" >/dev/null; then
  tests+=(tests)
fi
while IFS= read -r pkg; do
  [ -n "$pkg" ] || continue
  git rev-parse --verify --quiet "${head}:${pkg}/tests" >/dev/null || continue
  if ! git rev-parse --verify --quiet "${base}:${pkg}/src" >/dev/null; then
    echo "warning: ${pkg} has no src/ in ${base}; its tests are left out" >&2
    continue
  fi
  # A top-level entry of src/ that BASE lacks would be imported from the checkout.
  new=$(comm -23 <(git ls-tree --name-only "${head}:${pkg}/src" | sort) \
                 <(git ls-tree --name-only "${base}:${pkg}/src" | sort) | tr '\n' ' ')
  if [ -n "$new" ]; then
    echo "warning: ${pkg}/src has ${new% } in ${head} and not in ${base}; its tests are left out" >&2
    continue
  fi
  tests+=("${pkg}/tests")
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

# HEAD's test directories are pytest's testpaths, so they run when no test path is given and a
# path argument (or an option whose value is a path) narrows the run instead of being added to.
echo "tests of ${head} against the source of ${base}" >&2
cd "$work"
PYTHONPATH="${pythonpath}${PYTHONPATH:+:${PYTHONPATH}}" \
UV_PROJECT_ENVIRONMENT="$venv" UV_NO_SYNC=1 \
  "$python" -X dev -X warn_default_encoding -m pytest -p no:cacheprovider -rA --no-header --continue-on-collection-errors \
  -o "testpaths=${tests[*]}" "$@"
