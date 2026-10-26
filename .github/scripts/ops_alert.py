#!/usr/bin/env python3
"""Page the ops Telegram channel when a watched workflow keeps failing.

Runs from .github/workflows/ops_alert.yml after each watched run completes.
It keeps no state: every decision comes from the workflow's own run history.

- A single failure posts nothing; one-off failures clear on the next run.
- A failure streak that reaches Policy.min_failures runs AND spans
  Policy.min_span posts one alert, led by a verdict from ops_alert_rules.py.
- While the streak lasts, one reminder a day: the first failed run that is
  past Policy.remind_hour_utc and at least Policy.remind_quiet after the
  alert.
- The first success after an alerted streak posts a recovery notice.
- A re-run attempt never alerts or reminds (its first attempt already
  decided), but a re-run that passes can post the recovery notice.
- Only scheduled and manually dispatched runs from this repository count;
  anything else, such as a fork's pull request, is ignored.

To watch another workflow, add its name to ops_alert.yml's workflow_run
list, and to POLICIES below if the defaults don't fit its schedule. To
teach it a new kind of failure, add a rule to ops_alert_rules.py.

Missing secrets or a Telegram outage only log a warning. They never change
the watched run's result and never fail this one.

Usage: ops_alert.py --run-id <id> [--repo owner/name] [--dry-run]
"""
import argparse
import dataclasses
import datetime
import enum
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

from ops_alert_rules import RULES, VERDICT_LABELS, Verdict

FAILED = {"failure", "timed_out", "startup_failure"}
PASSED = {"success"}
# Any other conclusion (cancelled, skipped, neutral, ...) says nothing about
# whether the workflow works, so it neither extends nor breaks a streak.

MAX_PAGES = 10  # 100 runs a page; import_fancons is hourly, so ~40 days
LOG_LINE_MAX = 200


@dataclasses.dataclass(frozen=True)
class Policy:
    min_failures: int = 2
    # Under 6 hours: GitHub starts scheduled runs late by varying amounts, so
    # two consecutive 6-hourly runs can start as little as ~5h40m apart.
    min_span: datetime.timedelta = datetime.timedelta(hours=5)
    remind_hour_utc: int = 16
    remind_quiet: datetime.timedelta = datetime.timedelta(hours=12)


# Per-workflow overrides, keyed by workflow name. The default suits hourly,
# 6-hourly, and daily schedules alike: it waits 5 hours, or 2 runs, whichever
# is later.
POLICIES: dict[str, Policy] = {}


@dataclasses.dataclass(frozen=True)
class Run:
    id: int
    conclusion: str
    created_at: datetime.datetime
    url: str = ""


class Kind(enum.Enum):
    ALERT = "alert"
    REMIND = "remind"
    RECOVERED = "recovered"


@dataclasses.dataclass(frozen=True)
class Event:
    kind: Kind
    streak: list  # failed Runs, newest first
    run: Run  # the run this event is about


def _counted(runs):
    return [r for r in runs if r.conclusion in FAILED | PASSED]


def _leading_failures(runs):
    streak = []
    for r in runs:
        if r.conclusion not in FAILED:
            break
        streak.append(r)
    return streak


def _qualifies(streak, policy):
    return (
        len(streak) >= policy.min_failures
        and streak[0].created_at - streak[-1].created_at >= policy.min_span
    )


def _remind_day(t, policy):
    # The "day" rolls over at remind_hour_utc, so comparing two runs' days
    # tells whether that hour fell between them.
    return (t - datetime.timedelta(hours=policy.remind_hour_utc)).date()


def decide(runs, run_id, policy=Policy(), attempt=1):
    """Return the Event that run `run_id` should post, or None.

    `runs` is the workflow's completed runs, newest first. Runs newer than
    `run_id` are ignored, so the decision is the same whenever it is made.
    `attempt` is the run's attempt number; above 1, it is a re-run.
    """
    idx = next((i for i, r in enumerate(runs) if r.id == run_id), None)
    if idx is None:
        return None
    this = runs[idx]
    if this.conclusion not in FAILED | PASSED:
        return None
    history = _counted(runs[idx:])

    if this.conclusion in PASSED:
        streak = _leading_failures(history[1:])
        if attempt > 1:
            # The earlier attempt failed, so the streak ran up to this run.
            streak = [dataclasses.replace(this, conclusion="failure")] + streak
        if _qualifies(streak, policy):
            return Event(Kind.RECOVERED, streak, this)
        return None
    if attempt > 1:
        # The first attempt already alerted or reminded if it was due.
        return None

    streak = _leading_failures(history)
    if not _qualifies(streak, policy):
        return None
    # The run at which the streak first qualified is the one that alerted.
    alerted = next(
        i for i in range(len(streak) - 1, -1, -1) if _qualifies(streak[i:], policy)
    )
    if alerted == 0:
        return Event(Kind.ALERT, streak, this)
    alert_at = streak[alerted].created_at

    def due(t):
        # Past a reminder hour since the alert, and out of its quiet period.
        return (
            _remind_day(t, policy) > _remind_day(alert_at, policy)
            and t - alert_at >= policy.remind_quiet
        )

    # The first due run in each day sends that day's reminder.
    previous = streak[1].created_at
    already = due(previous) and _remind_day(previous, policy) == _remind_day(
        this.created_at, policy
    )
    if due(this.created_at) and not already:
        return Event(Kind.REMIND, streak, this)
    return None


@dataclasses.dataclass(frozen=True)
class Diagnosis:
    verdict: Verdict
    advice: list  # one line per matched rule, most severe first
    evidence: str  # the log line behind the leading verdict, or ""
    rule_ids: tuple = ()  # the matched rules, in advice order


_LOG_PREFIX = re.compile(r"^[^\t]*\t[^\t]*\t(?:\S+Z )?")
_STEP_START = "##[group]Run "
_STEP_FAILED = "##[error]Process completed with exit code"
_COMMAND_ECHO = "[36;1m"  # the colour code on a step header's script lines


def _clean(line):
    line = _LOG_PREFIX.sub("", line).strip()
    line = line.removeprefix("##[error]")
    return line[: LOG_LINE_MAX - 1] + "…" if len(line) > LOG_LINE_MAX else line


def _failing_step(lines):
    """The failing step and the steps after it (the failure handlers).

    `gh run view --log-failed` returns the whole job log, including steps
    that succeeded, such as an importer that skipped a 403ing source.
    Without step markers or an exit-code line, the whole log is all we have.
    """
    start = None
    for i, l in enumerate(lines):
        if _STEP_START in l:
            start = i
        elif _STEP_FAILED in l and start is not None:
            return lines[start:]
    return lines


def classify(log, rules=RULES):
    """Match a failed run's log against the rules."""
    if not log.strip():
        return Diagnosis(Verdict.UNCLEAR, ["Couldn't read the run's log."], "")
    lines = _failing_step(log.splitlines())
    # A step's header echoes its script, such as `echo "::error::..."`; match
    # what the step printed instead, so the evidence is the message itself.
    output = [l for l in lines if _STEP_START not in l and _COMMAND_ECHO not in l]
    hits = []
    for rule in rules:
        line = next((l for l in output if rule.pattern.search(l)), None)
        if line is not None:
            hits.append((rule, line))
    if not hits:
        last_error = next(
            (l for l in reversed(lines) if "##[error]" in l and "exit code" not in l),
            None,
        ) or next((l for l in reversed(lines) if l.strip() and _STEP_FAILED not in l), "")
        return Diagnosis(
            Verdict.UNCLEAR,
            ["No known failure pattern matched the log."],
            _clean(last_error),
        )
    # sorted() is stable, so rules of equal severity keep their file order.
    hits.sort(key=lambda h: h[0].verdict, reverse=True)
    return Diagnosis(
        hits[0][0].verdict,
        [r.advice for r, _ in hits],
        _clean(hits[0][1]),
        tuple(r.id for r, _ in hits),
    )


def _when(t):
    return t.strftime("%Y-%m-%d %H:%M")


def format_message(workflow, event, diagnosis=None, truncated=False):
    # The verdict label leads, so a phone's notification preview shows it.
    n = f"{len(event.streak)}+" if truncated else str(len(event.streak))
    runs = "run" if n == "1" else "runs"
    start = _when(event.streak[-1].created_at)
    if event.kind is Kind.RECOVERED:
        end = _when(event.streak[0].created_at)
        return (
            f"No action needed: {workflow} is passing again "
            f"after {n} failed {runs} ({start} to {end} UTC).\n{event.run.url}"
        )
    label = VERDICT_LABELS[diagnosis.verdict]
    if event.kind is Kind.ALERT:
        head = f"{label}: {workflow} has failed {n} {runs} in a row since {start} UTC."
    else:
        head = f"{label}: {workflow} is still failing, {n} {runs} in a row since {start} UTC."
    parts = [head, ""]
    parts += [f"- {a}" for a in diagnosis.advice]
    if diagnosis.evidence:
        parts += [
            "",
            "Log (quoted from the run, may include text from source sites): "
            + diagnosis.evidence,
        ]
    parts += [event.run.url]
    return "\n".join(parts)


def send_telegram(text, token, chat, urlopen=urllib.request.urlopen):
    """Post to the ops channel. Never raises; returns whether it was delivered."""
    if not token or not chat:
        print("::warning::ops Telegram secrets are not set; no alert sent")
        return False
    body = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    try:
        with urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=body, timeout=20
        ) as resp:
            status = resp.status
    except Exception as e:  # noqa: BLE001 -- any failure here is log-only
        # str(e) can include the request URL, which holds the bot token.
        print(f"::warning::ops alert could not be delivered ({type(e).__name__})")
        return False
    if status != 200:
        print(f"::warning::ops alert could not be delivered (Telegram HTTP {status})")
        return False
    print("ops alert delivered")
    return True


def _gh(*args):
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout


def _parse_time(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _to_run(d):
    return Run(d["id"], d["conclusion"] or "", _parse_time(d["created_at"]), d["html_url"])


def fetch_runs(repo, workflow_id, branch, run_id, gh=_gh):
    """Completed runs, newest first, back to the first success before
    `run_id`'s failure streak. Returns (runs, truncated)."""
    runs = []
    for page in range(1, MAX_PAGES + 1):
        data = json.loads(
            gh(
                "api",
                f"repos/{repo}/actions/workflows/{workflow_id}/runs"
                f"?status=completed&branch={urllib.parse.quote(branch)}"
                f"&per_page=100&page={page}",
            )
        )
        batch = [_to_run(d) for d in data["workflow_runs"]]
        runs += batch
        idx = next((i for i, r in enumerate(runs) if r.id == run_id), len(runs))
        older = _counted(runs[idx:])
        # One success past the streak is enough: decide() looks no further.
        if any(r.conclusion in PASSED for r in older[1:]) or len(batch) < 100:
            return runs, False
    return runs, True


def fetch_failed_log(repo, run_id, gh=_gh):
    try:
        return gh("run", "view", str(run_id), "-R", repo, "--log-failed")
    except subprocess.CalledProcessError:
        return ""


def main(argv=None, gh=_gh, send=send_telegram, env=os.environ):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--run-id", type=int, required=True)
    p.add_argument("--repo", default=env.get("GITHUB_REPOSITORY"))
    p.add_argument("--dry-run", action="store_true", help="print, don't send")
    args = p.parse_args(argv)

    # A failing gh call raises, which fails this run: a red ops_alert run is
    # the visible sign that alerting itself is broken.
    run = json.loads(gh("api", f"repos/{args.repo}/actions/runs/{args.run_id}"))
    workflow = run["name"]
    # workflow_run matches by name, so a fork's pull request can trigger this
    # too, even from a branch it named main.
    head_repo = (run.get("head_repository") or {}).get("full_name")
    if run["event"] not in {"schedule", "workflow_dispatch"} or head_repo != args.repo:
        print(f"{workflow} run {args.run_id} is a {run['event']} run from {head_repo}; ignoring")
        return 0
    # The run's embedded repository object omits default_branch.
    branch = json.loads(gh("api", f"repos/{args.repo}"))["default_branch"]
    if run["head_branch"] != branch:
        print(f"{workflow} run {args.run_id} is on {run['head_branch']}, not {branch}; ignoring")
        return 0
    runs, truncated = fetch_runs(args.repo, run["workflow_id"], branch, args.run_id, gh=gh)
    if not any(r.id == args.run_id for r in runs):
        # The listing can lag behind a run that just completed.
        print(f"run {args.run_id} is not in the run listing yet; adding it")
        this = _to_run(run)
        at = next((i for i, r in enumerate(runs) if r.created_at <= this.created_at), len(runs))
        runs.insert(at, this)
    attempt = run.get("run_attempt", 1)
    event = decide(runs, args.run_id, POLICIES.get(workflow, Policy()), attempt)
    if event is None:
        print(f"{workflow} run {args.run_id} attempt {attempt} ({run['conclusion']}): nothing to post")
        return 0
    diagnosis = None
    if event.kind is not Kind.RECOVERED:
        diagnosis = classify(fetch_failed_log(args.repo, args.run_id, gh=gh))
    text = format_message(workflow, event, diagnosis, truncated)
    rules = f" [{', '.join(diagnosis.rule_ids)}]" if diagnosis and diagnosis.rule_ids else ""
    print(f"--- {event.kind.value}{rules} ---\n{text}\n---")
    if not args.dry_run:
        send(text, env.get("OPS_TOKEN"), env.get("OPS_CHAT"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
