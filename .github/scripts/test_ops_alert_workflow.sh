#!/usr/bin/env bash
#
# Tests the "Decide and post" step in .github/workflows/ops_alert.yml.
# Runs the step's real run: block (extracted from the YAML, never duplicated)
# under GitHub's shell with a stubbed python3, and checks that only a manual
# dispatch with dry_run left on adds --dry-run, so workflow_run triggers send,
# and that --run-attempt is passed only when RUN_ATTEMPT is set.
# Also checks that dry_run defaults on, that workflow_run fires only on
# completed runs, that RUN_ATTEMPT comes from the triggering workflow_run or
# else the run_attempt input, and that the step reads the same ops secrets as
# deploy.yml's trigger alert.
#
# Needs PyYAML: uv run -q --no-project --with pyyaml -- bash .github/scripts/test_ops_alert_workflow.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKFLOW="$REPO_ROOT/.github/workflows/ops_alert.yml"
DEPLOY="$REPO_ROOT/.github/workflows/deploy.yml"
STEP="Decide and post"
DEPLOY_STEP="Alert ops channel (trigger failed)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

FAILURES=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAILURES=$((FAILURES + 1)); }

python3 - "$WORKFLOW" "$STEP" >"$TMP/run.sh" <<'PY'
import sys, yaml
wf = yaml.safe_load(open(sys.argv[1]))
for step in wf["jobs"]["alert"]["steps"]:
    if step.get("name") == sys.argv[2]:
        sys.stdout.write(step["run"])
        sys.exit(0)
sys.stderr.write("step not found: %s\n" % sys.argv[2])
sys.exit(2)
PY
[ -s "$TMP/run.sh" ] || { echo "FAIL: extracted run block is empty"; exit 1; }
if bash -n "$TMP/run.sh"; then pass "run block parses"; else fail "run block does not parse"; fi

# python3 stub: records its arguments, one per line.
mkdir -p "$TMP/stub"
cat >"$TMP/stub/python3" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$STUB_ARGS"
SH
chmod +x "$TMP/stub/python3"

# Runs the block; python3's arguments land in $TMP/args.
run() {
  : >"$TMP/args"
  env STUB_ARGS="$TMP/args" RUN_ID=12345 "$@" PATH="$TMP/stub:$PATH" \
    bash --noprofile --norc -eo pipefail "$TMP/run.sh" >/dev/null 2>&1 \
    || fail "run block exited nonzero with $*"
}

run DRY_RUN= RUN_ATTEMPT=
if grep -qx -- "--dry-run" "$TMP/args"; then
  fail "workflow_run (DRY_RUN empty): passes --dry-run"
else
  pass "workflow_run (DRY_RUN empty): sends"
fi
if grep -A1 -x -- "--run-id" "$TMP/args" | grep -qx 12345; then
  pass "passes --run-id \$RUN_ID"
else
  fail "does not pass --run-id \$RUN_ID"
fi
if grep -qx -- "--run-attempt" "$TMP/args"; then
  fail "RUN_ATTEMPT empty: passes --run-attempt"
else
  pass "RUN_ATTEMPT empty: omits --run-attempt"
fi

run DRY_RUN= RUN_ATTEMPT=3
if grep -A1 -x -- "--run-attempt" "$TMP/args" | grep -qx 3; then
  pass "RUN_ATTEMPT=3: passes --run-attempt 3"
else
  fail "RUN_ATTEMPT=3: does not pass --run-attempt 3"
fi

run DRY_RUN=false
if grep -qx -- "--dry-run" "$TMP/args"; then
  fail "DRY_RUN=false: passes --dry-run"
else
  pass "DRY_RUN=false: sends"
fi

run DRY_RUN=true
if grep -qx -- "--dry-run" "$TMP/args"; then
  pass "DRY_RUN=true: passes --dry-run"
else
  fail "DRY_RUN=true: no --dry-run"
fi

if python3 - "$WORKFLOW" "$STEP" "$DEPLOY" "$DEPLOY_STEP" <<'PY'
import sys, yaml

def step(path, job, name):
    wf = yaml.safe_load(open(path))
    return wf, next(s for s in wf["jobs"][job]["steps"] if s.get("name") == name)

wf, ops = step(sys.argv[1], "alert", sys.argv[2])
_, deploy = step(sys.argv[3], "deploy", sys.argv[4])
ok = True
# PyYAML reads the bare key `on` as True.
on = wf.get("on", wf.get(True))
dry_run = on["workflow_dispatch"]["inputs"]["dry_run"]
if dry_run.get("default") is not True:
    print("FAIL: workflow_dispatch input dry_run does not default to true")
    ok = False
if on["workflow_run"].get("types") != ["completed"]:
    print(f"FAIL: workflow_run types are {on['workflow_run'].get('types')!r}, not ['completed']")
    ok = False
attempt = "${{ github.event.workflow_run.run_attempt || inputs.run_attempt }}"
if ops["env"].get("RUN_ATTEMPT") != attempt:
    print(f"FAIL: RUN_ATTEMPT is {ops['env'].get('RUN_ATTEMPT')!r}, not {attempt!r}")
    ok = False
for var in ("OPS_TOKEN", "OPS_CHAT"):
    if ops["env"].get(var) != deploy["env"][var]:
        print(f"FAIL: {var} is {ops['env'].get(var)!r}, deploy.yml uses {deploy['env'][var]!r}")
        ok = False
sys.exit(0 if ok else 1)
PY
then
  pass "dry_run defaults to true; workflow_run fires on completed; RUN_ATTEMPT is the workflow_run's or the input's; OPS_TOKEN and OPS_CHAT match deploy.yml's trigger alert"
else
  FAILURES=$((FAILURES + 1))
fi

echo
if [ "$FAILURES" -ne 0 ]; then
  echo "$FAILURES assertion(s) FAILED"
  exit 1
fi
echo "all ops-alert workflow assertions passed"
