#!/usr/bin/env bash
#
# Self-guard for the "Alert ops channel (trigger failed)" step in
# .github/workflows/deploy.yml. Runs the step's real run: block (extracted from
# the YAML, never duplicated) under GitHub's shell with a stubbed curl, and
# checks that it pages when it can and always exits 0, so alerting problems
# never change the deploy's red/green.
#
# Needs PyYAML: uv run -q --no-project --with pyyaml -- bash .github/scripts/test_deploy_alert.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKFLOW="$REPO_ROOT/.github/workflows/deploy.yml"
STEP="Alert ops channel (trigger failed)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP" /tmp/tg_resp' EXIT

FAILURES=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAILURES=$((FAILURES + 1)); }

python3 - "$WORKFLOW" "$STEP" >"$TMP/run.sh" <<'PY'
import sys, yaml
wf = yaml.safe_load(open(sys.argv[1]))
for step in wf["jobs"]["deploy"]["steps"]:
    if step.get("name") == sys.argv[2]:
        sys.stdout.write(step["run"])
        sys.exit(0)
sys.stderr.write("step not found: %s\n" % sys.argv[2])
sys.exit(2)
PY
[ -s "$TMP/run.sh" ] || { echo "FAIL: extracted run block is empty"; exit 1; }
bash -n "$TMP/run.sh" && pass "run block parses" || fail "run block does not parse"

# curl stub: records args, writes the -o file, prints STUB_CURL_CODE as
# %{http_code}, and exits STUB_CURL_EXIT (a transport failure).
mkdir -p "$TMP/stub"
cat >"$TMP/stub/curl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_ARGS"
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
[ -n "$out" ] && : >"$out"
printf '%s' "${STUB_CURL_CODE:-200}"
exit "${STUB_CURL_EXIT:-0}"
SH
chmod +x "$TMP/stub/curl"

# Runs the block; prints its exit code; curl args land in $TMP/args.
run() {
  : >"$TMP/args"
  local rc=0
  env STUB_ARGS="$TMP/args" RUN_URL="https://example.invalid/run/1" "$@" PATH="$TMP/stub:$PATH" \
    bash --noprofile --norc -eo pipefail "$TMP/run.sh" >/dev/null 2>&1 || rc=$?
  echo "$rc"
}

rc=$(run OPS_TOKEN=t OPS_CHAT=c STUB_CURL_CODE=200)
[ "$rc" = 0 ] && pass "configured: exits 0" || fail "configured: exit $rc"
grep -q "trigger FAILED" "$TMP/args" && grep -q "chat_id=c" "$TMP/args" && grep -q "example.invalid/run/1" "$TMP/args" \
  && pass "configured: sends the failure alert with the run link to the ops chat" \
  || fail "configured: no alert sent to the ops chat"

rc=$(run OPS_TOKEN= OPS_CHAT=)
[ "$rc" = 0 ] && pass "secrets unset: exits 0" || fail "secrets unset: exit $rc"
[ -s "$TMP/args" ] && fail "secrets unset: should not call Telegram" || pass "secrets unset: sends nothing"

rc=$(run OPS_TOKEN=t OPS_CHAT=c STUB_CURL_CODE=500)
[ "$rc" = 0 ] && pass "Telegram 500: exits 0" || fail "Telegram 500: exit $rc"

rc=$(run OPS_TOKEN=t OPS_CHAT=c STUB_CURL_EXIT=7)
[ "$rc" = 0 ] && pass "curl transport failure: exits 0" || fail "curl transport failure: exit $rc"

echo
if [ "$FAILURES" -ne 0 ]; then
  echo "$FAILURES assertion(s) FAILED"
  exit 1
fi
echo "all deploy-alert assertions passed"
