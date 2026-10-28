#!/usr/bin/env python3
"""Page the ops Telegram channel when a watched workflow keeps failing.

Runs from .github/workflows/ops_alert.yml after each watched run completes.
It keeps no state: every decision comes from the workflow's own run history.

- Each completed attempt of a run is one step in the history, taken in the
  order the attempts finished. A re-run is just another attempt.
- A single failure posts nothing; one-off failures clear on the next pass.
- A failure streak that reaches Policy.min_failures attempts AND spans
  Policy.min_span posts one alert, led by a verdict from ops_alert_rules.py.
- While the streak lasts, one reminder a day: the first failure that is
  past Policy.remind_hour_utc and at least Policy.remind_quiet after the
  alert.
- The first pass after an alerted streak posts a recovery notice.
- Attempts that neither passed nor failed, such as cancelled ones, don't
  count. Neither does an attempt from a run older than the last pass or
  one of the streak's failures, such as a re-run of an earlier commit or
  a hung run that times out after a recovery, nor an attempt that
  finished more than LOOKBACK after its run was created.
- Only scheduled and manually dispatched runs from this repository count;
  anything else, such as a fork's pull request, is ignored.

The job for each attempt replays every attempt that finished before it,
so every job agrees on what was already posted. Known limit: that holds
only if the run listing is current. An attempt that finished seconds
before this one but still shows as in progress is left out, which can
post a second alert or reminder, or miss a recovery.

To watch another workflow, add its name to ops_alert.yml's workflow_run
list, and to POLICIES below if the defaults don't fit its schedule. To
teach it a new kind of failure, add a rule to ops_alert_rules.py.

Missing secrets or a Telegram outage only log a warning. They never change
the watched run's result and never fail this one.

Usage: ops_alert.py --run-id <id> [--run-attempt <n>] [--repo owner/name] [--dry-run]
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
import urllib.error
import urllib.parse
import urllib.request

from ops_alert_rules import RULES, VERDICT_LABELS, Verdict

FAILED = {"failure", "timed_out", "startup_failure"}
PASSED = {"success"}
# Any other conclusion (cancelled, skipped, neutral, ...) says nothing about
# whether the workflow works, so it neither extends nor breaks a streak.

MAX_PAGES = 10  # 100 runs a page; import_fancons is hourly, so ~40 days
# How long after its run was created an attempt still counts. It covers a
# run that hangs until GitHub's 6-hour job limit and a same-day re-run, and
# bounds how far back the run listing must be read.
LOOKBACK = datetime.timedelta(days=1)
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
    """One completed attempt of a workflow run."""
    id: int
    conclusion: str
    created_at: datetime.datetime  # when the run, not this attempt, was created
    completed_at: datetime.datetime
    url: str = ""
    attempt: int = 1

    @property
    def key(self):
        # Attempts that finish in the same second still sort the same way
        # in every job.
        return (self.completed_at, self.id, self.attempt)


class Kind(enum.Enum):
    ALERT = "alert"
    REMIND = "remind"
    RECOVERED = "recovered"


@dataclasses.dataclass(frozen=True)
class Event:
    kind: Kind
    streak: list  # failed attempts, newest first
    run: Run  # the attempt this event is about


def _counts(r):
    return r.conclusion in FAILED | PASSED and r.completed_at - r.created_at <= LOOKBACK


def _qualifies(streak, policy):
    return (
        len(streak) >= policy.min_failures
        and streak[0].completed_at - streak[-1].completed_at >= policy.min_span
    )


def _remind_day(t, policy):
    # The "day" rolls over at remind_hour_utc, so comparing two attempts'
    # days tells whether that hour fell between them.
    return (t - datetime.timedelta(hours=policy.remind_hour_utc)).date()


def _replay(runs, policy):
    """Yield (attempt, Event or None, reset) for each attempt that counts,
    in the order they finished. `reset` is whether it ended the streak."""
    streak, alert_at, reminded, passed = [], None, None, None
    for r in sorted(filter(_counts, runs), key=lambda r: r.key):
        newest = max([f.created_at for f in streak] + [passed or r.created_at])
        if r.created_at < newest:
            # An older run's result, such as a re-run of an earlier commit,
            # says nothing about whether the newer runs' code works.
            yield r, None, False
            continue
        if r.conclusion in PASSED:
            event = Event(Kind.RECOVERED, streak, r) if alert_at else None
            streak, alert_at, reminded, passed = [], None, None, r.created_at
            yield r, event, True
            continue
        streak = [r] + streak
        event = None
        if alert_at is None:
            if _qualifies(streak, policy):
                alert_at = r.completed_at
                event = Event(Kind.ALERT, streak, r)
        elif (
            _remind_day(r.completed_at, policy) > _remind_day(alert_at, policy)
            and r.completed_at - alert_at >= policy.remind_quiet
            and _remind_day(r.completed_at, policy) != reminded
        ):
            reminded = _remind_day(r.completed_at, policy)
            event = Event(Kind.REMIND, streak, r)
        yield r, event, False


def decide(runs, this, policy=Policy()):
    """Return the Event that attempt `this` should post, or None.

    `runs` is the workflow's completed attempts, in any order. Only those
    that finished before `this` matter, so the decision is the same
    whenever it is made.
    """
    earlier = [r for r in runs if r.key < this.key]
    for r, event, _ in _replay(earlier + [this], policy):
        if r == this:
            return event
    return None


@dataclasses.dataclass(frozen=True)
class Diagnosis:
    verdict: Verdict
    advice: list  # one line per matched rule, most severe first
    evidence: str  # the log line behind the leading verdict, or ""
    rule_ids: tuple = ()  # the matched rules, in advice order


_LOG_PREFIX = re.compile(r"^[^\t]*\t[^\t]*\t(?:\S+Z )?")
_STEP_START = "##[group]Run "
# The lines that end a failing step: a nonzero exit, or a timeout or
# cancellation that stopped it.
_STEP_FAILED = (
    "##[error]Process completed with exit code",
    "##[error]The operation was canceled",
    "has exceeded the maximum execution time",
)
_COMMAND_ECHO = "[36;1m"  # the colour code on a step header's script lines


def _clean(line):
    line = _LOG_PREFIX.sub("", line).strip()
    line = line.removeprefix("##[error]")
    return line[: LOG_LINE_MAX - 1] + "…" if len(line) > LOG_LINE_MAX else line


def _ends_step(line):
    return any(marker in line for marker in _STEP_FAILED)


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
        elif _ends_step(l) and start is not None:
            return lines[start:]
    return lines


def _last_error(lines):
    """The last error line before the failing step ended, else the last line.

    Searching back from the step's end skips the failure handlers and the
    post-job cleanup that follow it in the log.
    """
    end = len(lines)
    for i, l in enumerate(lines):
        if _ends_step(l):
            end = i
            break
    for l in reversed(lines[:end]):
        if "##[error]" in l:
            return l
    for l in reversed(lines[:end]):
        if l.strip():
            return l
    return ""


def classify(log, rules=RULES):
    """Match a failed run's log against the rules."""
    if not log.strip():
        return Diagnosis(
            Verdict.UNCLEAR,
            ["Couldn't read the run's log. Open the run to see which step failed."],
            "",
        )
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
        return Diagnosis(
            Verdict.UNCLEAR,
            ["No known failure pattern matched. Open the run and read the "
             "failing step's errors."],
            _clean(_last_error(lines)),
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
            f"after {n} failed {runs} ({start} to {end} UTC).\nRun: {event.run.url}"
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
            "Log line (may include text from source sites): " + diagnosis.evidence,
        ]
    parts += ["Run: " + event.run.url]
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
    except urllib.error.HTTPError as e:
        # Telegram's error replies (400, 401, 429) arrive as HTTPError.
        status = e.code
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


def _to_run(d, created_at):
    """The Run for an API run or attempt object. An attempt object's
    created_at is the attempt's own start, so the run's is passed in."""
    return Run(
        d["id"], d["conclusion"] or "", created_at, _parse_time(d["updated_at"]),
        d["html_url"], d.get("run_attempt", 1),
    )


def _watched(d, repo):
    """Whether an API run object counts: a scheduled or dispatched run from
    `repo` itself. A fork's pull request doesn't count, even from a branch
    named main."""
    head_repo = (d.get("head_repository") or {}).get("full_name")
    return d["event"] in {"schedule", "workflow_dispatch"} and head_repo == repo


def _attempt(repo, run_id, k, gh):
    return json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}/attempts/{k}"))


def _listed_attempts(d, repo, gh):
    """The completed attempts of a listing entry. The entry shows only its
    latest attempt; earlier ones have all completed."""
    created_at = _parse_time(d["created_at"])
    latest = d.get("run_attempt", 1)
    found = [_to_run(_attempt(repo, d["id"], k, gh), created_at) for k in range(1, latest)]
    if d["status"] == "completed":
        found.append(_to_run(d, created_at))
    return found


def fetch_runs(repo, workflow_id, branch, this, gh=_gh):
    """Completed attempts, back far enough to include the last pass that
    ended a streak before `this`, the attempt being decided. Returns
    (attempts, truncated)."""
    found = {this.key[1:]: this}
    for page in range(1, MAX_PAGES + 1):
        data = json.loads(
            gh(
                "api",
                f"repos/{repo}/actions/workflows/{workflow_id}/runs"
                f"?branch={urllib.parse.quote(branch)}"
                f"&per_page=100&page={page}",
            )
        )
        batch = data["workflow_runs"]
        for d in batch:
            if _watched(d, repo):
                for r in _listed_attempts(d, repo, gh):
                    # Keyed by (run, attempt): a run that shifts onto the next
                    # page between calls is listed twice. `this` is already
                    # known, and the listing can lag behind it.
                    found.setdefault(r.key[1:], r)
        runs = list(found.values())
        if len(batch) < 100:
            return runs, False
        earlier = [r for r in runs if r.key < this.key]
        resets = [r for r, _, reset in _replay(earlier, Policy()) if reset]
        # Any attempt that finished after the last reset belongs to a run
        # created at most LOOKBACK before it.
        if resets and _parse_time(batch[-1]["created_at"]) < resets[-1].completed_at - LOOKBACK:
            return runs, False
    return runs, True


def fetch_failed_log(repo, run_id, attempt, gh=_gh):
    try:
        return gh("run", "view", str(run_id), "-R", repo, "--attempt", str(attempt),
                  "--log-failed")
    except subprocess.CalledProcessError:
        return ""


def main(argv=None, gh=_gh, send=send_telegram, env=os.environ):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--run-id", type=int, required=True)
    # The attempt that triggered this job. The run itself reports only its
    # latest attempt.
    p.add_argument("--run-attempt", type=int)
    p.add_argument("--repo", default=env.get("GITHUB_REPOSITORY"))
    p.add_argument("--dry-run", action="store_true", help="print, don't send")
    args = p.parse_args(argv)

    # A failing gh call raises, which fails this run: a red ops_alert run is
    # the visible sign that alerting itself is broken.
    run = json.loads(gh("api", f"repos/{args.repo}/actions/runs/{args.run_id}"))
    workflow = run["name"]
    # workflow_run matches by name, so a fork's pull request can trigger this
    # too, even from a branch it named main. This check is the only filter:
    # ops_alert.yml has none.
    if not _watched(run, args.repo):
        print(f"{workflow} run {args.run_id} ({run['event']}) is not a scheduled or "
              f"dispatched run from {args.repo}; ignoring")
        return 0
    # The run's embedded repository object omits default_branch.
    branch = json.loads(gh("api", f"repos/{args.repo}"))["default_branch"]
    if run["head_branch"] != branch:
        print(f"{workflow} run {args.run_id} is on {run['head_branch']}, not {branch}; ignoring")
        return 0
    # Judge the triggering attempt, even if a newer one has started. The
    # run object shows only its latest attempt.
    attempt = args.run_attempt or run.get("run_attempt", 1)
    done = run
    if attempt != run.get("run_attempt", 1):
        done = _attempt(args.repo, args.run_id, attempt, gh)
    this = dataclasses.replace(
        _to_run(done, _parse_time(run["created_at"])),
        # An attempt's html_url is the run's, so name the attempt in the link.
        url=f"{run['html_url']}/attempts/{attempt}", attempt=attempt,
    )
    runs, truncated = fetch_runs(args.repo, run["workflow_id"], branch, this, gh=gh)
    event = decide(runs, this, POLICIES.get(workflow, Policy()))
    if event is None:
        print(f"{workflow} run {args.run_id} attempt {attempt} ({done['conclusion']}): nothing to post")
        return 0
    diagnosis = None
    if event.kind is not Kind.RECOVERED:
        diagnosis = classify(fetch_failed_log(args.repo, args.run_id, attempt, gh=gh))
    text = format_message(workflow, event, diagnosis, truncated)
    rules = f" [{', '.join(diagnosis.rule_ids)}]" if diagnosis and diagnosis.rule_ids else ""
    print(f"--- {event.kind.value}{rules} ---\n{text}\n---")
    if not args.dry_run:
        send(text, env.get("OPS_TOKEN"), env.get("OPS_CHAT"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
