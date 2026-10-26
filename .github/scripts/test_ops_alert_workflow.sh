#!/usr/bin/env bash
#
# Self-guard for the "Decide and post" step in .github/workflows/ops_alert.yml.
# Runs the step's real run: block (extracted from the YAML, never duplicated)
# under GitHub's shell with a stubbed python3, and checks that only a manual
# dispatch with dry_run left on adds --dry-run, so workflow_run triggers send.
# Also checks that dry_run defaults on and that the step reads the same ops
# secrets as deploy.yml's trigger alert.
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
bash -n "$TMP/run.sh" && pass "run block parses" || fail "run block does not parse"

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

run DRY_RUN=
grep -qx -- "--dry-run" "$TMP/args" && fail "workflow_run (DRY_RUN empty): passes --dry-run" \
  || pass "workflow_run (DRY_RUN empty): sends"
grep -A1 -x -- "--run-id" "$TMP/args" | grep -qx 12345 \
  && pass "passes --run-id \$RUN_ID" || fail "does not pass --run-id \$RUN_ID"

run DRY_RUN=false
grep -qx -- "--dry-run" "$TMP/args" && fail "DRY_RUN=false: passes --dry-run" \
  || pass "DRY_RUN=false: sends"

run DRY_RUN=true
grep -qx -- "--dry-run" "$TMP/args" && pass "DRY_RUN=true: passes --dry-run" \
  || fail "DRY_RUN=true: no --dry-run"

if python3 - "$WORKFLOW" "$STEP" "$DEPLOY" "$DEPLOY_STEP" <<'PY'
import sys, yaml

def step(path, job, name):
    wf = yaml.safe_load(open(path))
    return wf, next(s for s in wf["jobs"][job]["steps"] if s.get("name") == name)

wf, ops = step(sys.argv[1], "alert", sys.argv[2])
_, deploy = step(sys.argv[3], "deploy", sys.argv[4])
ok = True
# PyYAML reads the bare key `on` as True.
dry_run = wf.get("on", wf.get(True))["workflow_dispatch"]["inputs"]["dry_run"]
if dry_run.get("default") is not True:
    print("FAIL: workflow_dispatch input dry_run does not default to true")
    ok = False
for var in ("OPS_TOKEN", "OPS_CHAT"):
    if ops["env"].get(var) != deploy["env"][var]:
        print(f"FAIL: {var} is {ops['env'].get(var)!r}, deploy.yml uses {deploy['env'][var]!r}")
        ok = False
sys.exit(0 if ok else 1)
PY
then
  pass "dry_run defaults to true; OPS_TOKEN and OPS_CHAT match deploy.yml's trigger alert"
else
  FAILURES=$((FAILURES + 1))
fi

echo
if [ "$FAILURES" -ne 0 ]; then
  echo "$FAILURES assertion(s) FAILED"
  exit 1
fi
echo "all ops-alert workflow assertions passed"
