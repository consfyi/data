#!/usr/bin/env bash
#
# Self-guard for the "Fail if any source failed" step in
# .github/workflows/import_concat.yml. Runs the step's real run: block
# (extracted from the YAML, never duplicated) and checks that it fails the run
# exactly when import_concat_all.sh reported a failed source, and that it runs
# after the commit, so the sources that worked still land on main.
#
# Needs PyYAML: uv run -q --no-project --with pyyaml -- bash .github/scripts/test_concat_failures_step.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKFLOW="$REPO_ROOT/.github/workflows/import_concat.yml"
STEP="Fail if any source failed"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

FAILURES=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAILURES=$((FAILURES + 1)); }

# Prints the step's run block, and checks its place in the job: the import
# step must write the file this step reads, and the push must come first.
python3 - "$WORKFLOW" "$STEP" >"$TMP/run.sh" <<'PY'
import sys, yaml
steps = yaml.safe_load(open(sys.argv[1]))["jobs"]["import"]["steps"]
names = [s.get("name") for s in steps]
if sys.argv[2] not in names:
    sys.exit("step not found: %s" % sys.argv[2])
i = names.index(sys.argv[2])
importer = next(s for s in steps if "import_concat_all.sh" in s.get("run", ""))
if importer.get("env", {}).get("CONCAT_FAILURES_FILE") != "${{ runner.temp }}/concat_failures.txt":
    sys.exit("the import step doesn't write the file this step reads")
push = next(j for j, s in enumerate(steps) if "git push" in s.get("run", ""))
if push > i:
    sys.exit("the step runs before the push, so a failed source would block the rest")
sys.stdout.write(steps[i]["run"])
PY
[ -s "$TMP/run.sh" ] && pass "step found after the push" || fail "extracted run block is empty"
bash -n "$TMP/run.sh" && pass "run block parses" || fail "run block does not parse"

# Runs the block with RUNNER_TEMP=$TMP/runner; prints its exit code.
mkdir -p "$TMP/runner"
run() {
  local rc=0
  RUNNER_TEMP="$TMP/runner" bash --noprofile --norc -eo pipefail "$TMP/run.sh" >"$TMP/out" 2>&1 || rc=$?
  echo "$rc"
}

rm -f "$TMP/runner/concat_failures.txt"
rc=$(run)
[ "$rc" = 0 ] && pass "no report file: passes" || fail "no report file: exit $rc"

: >"$TMP/runner/concat_failures.txt"
rc=$(run)
[ "$rc" = 0 ] && pass "empty report: passes" || fail "empty report: exit $rc"

printf '%s\n' \
  "palmetto-fur-the-season.json https://reg.palmettofurtheseason.org: httpx.HTTPStatusError: Client error '403 Forbidden'" \
  "furski.json https://reg.fur.ski: KeyError: 'conventions'" >"$TMP/runner/concat_failures.txt"
rc=$(run)
[ "$rc" = 1 ] && pass "two failures: fails the run" || fail "two failures: exit $rc"
grep -q "^::error::2 ConCat source(s) failed" "$TMP/out" && pass "counts the failures" || fail "no count line"
grep -q "^::error::palmetto-fur-the-season.json .*403 Forbidden" "$TMP/out" \
  && grep -q "^::error::furski.json .*KeyError" "$TMP/out" \
  && pass "names each source and its error" || fail "missing a source line: $(cat "$TMP/out")"

[ "$FAILURES" = 0 ] && echo "all passed" || { echo "$FAILURES failed"; exit 1; }
