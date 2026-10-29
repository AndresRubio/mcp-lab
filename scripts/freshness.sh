#!/usr/bin/env bash
# Compare the pinned protocol stack with the latest published releases: the MCP spec date the
# README targets, mcp and fastmcp in uv.lock, and the conformance and inspector versions in their
# scripts. Prints one line per pin, "stale: ..." for each one behind. Exits 1 when any pin is
# stale, 2 when a lookup fails, 0 when every pin is current. a2a-sdk is skipped until M4 pins it.
# Set GITHUB_TOKEN to raise the GitHub API rate limit.
#
# Usage: bash scripts/freshness.sh   (needs curl, npm and python3)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

stale=0

fail() {
  echo "lookup failed: $1" >&2
  exit 2
}

# Print the field at the Python subscript $1 of the JSON document on stdin.
json_field() {
  python3 -c "import json, sys; print(json.load(sys.stdin)$1)"
}

# compare NAME PINNED LATEST
compare() {
  if [ "$2" = "$3" ]; then
    echo "current: $1 $2"
  else
    echo "stale: $1 pinned $2, latest $3"
    stale=1
  fi
}

# The version uv.lock pins for the package named $1.
lock_version() {
  awk -v name="$1" '$0 == "name = \"" name "\"" { found = 1; next }
    found && /^version = / { gsub(/"/, "", $3); print $3; exit }' uv.lock
}

pypi_latest() {
  curl -fsS "https://pypi.org/pypi/$1/json" | json_field '["info"]["version"]'
}

for pkg in mcp fastmcp; do
  pinned=$(lock_version "$pkg")
  [ -n "$pinned" ] || fail "no $pkg entry in uv.lock"
  latest=$(pypi_latest "$pkg") || fail "PyPI version of $pkg"
  compare "$pkg" "$pinned" "$latest"
done

# npm_pin NAME VARIABLE FILE: the version FILE assigns to VARIABLE, compared with npm's latest.
npm_pin() {
  local pinned latest
  pinned=$(sed -n "s/^$2=\"\(.*\)\"\$/\1/p" "$3")
  [ -n "$pinned" ] || fail "no $2 in $3"
  latest=$(npm view "$1" version) || fail "npm version of $1"
  compare "$1" "$pinned" "$latest"
}

npm_pin @modelcontextprotocol/conformance CONFORMANCE_VERSION scripts/conformance.sh
npm_pin @modelcontextprotocol/inspector INSPECTOR_VERSION scripts/inspector-smoke.sh

# The spec repository tags each release with its date, the same string the README names.
spec=$(sed -n 's/.*MCP spec \([0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}\).*/\1/p' README.md | head -n 1)
[ -n "$spec" ] || fail "no 'MCP spec YYYY-MM-DD' in README.md"
auth="Accept: application/vnd.github+json"
if [ -n "${GITHUB_TOKEN:-}" ]; then
  auth="Authorization: Bearer ${GITHUB_TOKEN}"
fi
release=$(curl -fsS -H "$auth" \
  https://api.github.com/repos/modelcontextprotocol/modelcontextprotocol/releases/latest |
  json_field '["tag_name"]') || fail "latest release of modelcontextprotocol/modelcontextprotocol"
compare "MCP spec" "$spec" "$release"

exit "$stale"
