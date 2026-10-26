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

To watch another workflow, add its name to ops_alert.yml's workflow_run
list, and to POLICIES below if the defaults don't fit its schedule. To
teach it a new kind of failure, add a rule to ops_alert_rules.py.

Missing secrets or a Telegram outage degrade to a log warning: alerting
problems never turn the watched run's red/green, and never fail this one.

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
    min_span: datetime.timedelta = datetime.timedelta(hours=6)
    remind_hour_utc: int = 16
    remind_quiet: datetime.timedelta = datetime.timedelta(hours=12)


# Per-workflow overrides, keyed by workflow name. The default suits hourly,
# 6-hourly, and daily schedules alike: it waits 6 hours, or 2 runs, whichever
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


def decide(runs, run_id, policy=Policy()):
    """Return the Event that run `run_id` should post, or None.

    `runs` is the workflow's completed runs, newest first. Runs newer than
    `run_id` are ignored, so the decision is the same whenever it is made.
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
        if _qualifies(streak, policy):
            return Event(Kind.RECOVERED, streak, this)
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


_LOG_PREFIX = re.compile(r"^[^\t]*\t[^\t]*\t(?:\S+Z )?")


def _clean(line):
    line = _LOG_PREFIX.sub("", line).strip()
    return line[: LOG_LINE_MAX - 1] + "…" if len(line) > LOG_LINE_MAX else line


def classify(log, rules=RULES):
    """Match a failed run's log against the rules."""
    if not log.strip():
        return Diagnosis(Verdict.UNCLEAR, ["Couldn't read the run's log."], "")
    lines = log.splitlines()
    hits = []
    for rule in rules:
        line = next((l for l in lines if rule.pattern.search(l)), None)
        if line is not None:
            hits.append((rule, line))
    if not hits:
        last_error = next(
            (l for l in reversed(lines) if "##[error]" in l and "exit code" not in l),
            None,
        ) or next((l for l in reversed(lines) if l.strip()), "")
        return Diagnosis(
            Verdict.UNCLEAR,
            ["No known failure pattern matched the log."],
            _clean(last_error),
        )
    # sorted() is stable, so rules of equal severity keep their file order.
    hits.sort(key=lambda h: h[0].verdict, reverse=True)
    return Diagnosis(
        hits[0][0].verdict, [r.advice for r, _ in hits], _clean(hits[0][1])
    )


def _when(t):
    return t.strftime("%Y-%m-%d %H:%M")


def _count(streak, truncated):
    return f"{len(streak)}+" if truncated else str(len(streak))


def format_message(workflow, event, diagnosis=None, truncated=False):
    n = _count(event.streak, truncated)
    start = _when(event.streak[-1].created_at)
    if event.kind is Kind.RECOVERED:
        end = _when(event.streak[0].created_at)
        return (
            f"{workflow} is passing again after {n} failed runs "
            f"({start} to {end} UTC).\n{event.run.url}"
        )
    if event.kind is Kind.ALERT:
        head = f"{workflow} has failed {n} runs in a row, since {start} UTC."
    else:
        head = f"{workflow} is still failing: {n} runs in a row since {start} UTC."
    parts = [head, "", f"{VERDICT_LABELS[diagnosis.verdict]}."]
    parts += [f"- {a}" for a in diagnosis.advice]
    if diagnosis.evidence:
        parts += ["", f"Log: {diagnosis.evidence}"]
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
        older = _counted(runs[next((i for i, r in enumerate(runs) if r.id == run_id), len(runs)):])
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

    run = json.loads(gh("api", f"repos/{args.repo}/actions/runs/{args.run_id}"))
    workflow = run["name"]
    # The run's embedded repository object omits default_branch.
    branch = json.loads(gh("api", f"repos/{args.repo}"))["default_branch"]
    if run["head_branch"] != branch:
        print(f"{workflow} run {args.run_id} is on {run['head_branch']}, not {branch}; ignoring")
        return 0
    runs, truncated = fetch_runs(args.repo, run["workflow_id"], branch, args.run_id, gh=gh)
    event = decide(runs, args.run_id, POLICIES.get(workflow, Policy()))
    if event is None:
        print(f"{workflow} run {args.run_id} ({run['conclusion']}): nothing to post")
        return 0
    diagnosis = None
    if event.kind is not Kind.RECOVERED:
        diagnosis = classify(fetch_failed_log(args.repo, args.run_id, gh=gh))
    text = format_message(workflow, event, diagnosis, truncated)
    print(f"--- {event.kind.value} ---\n{text}\n---")
    if not args.dry_run:
        send(text, env.get("OPS_TOKEN"), env.get("OPS_CHAT"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
