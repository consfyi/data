#!/usr/bin/env python3
"""Handle a `/reject` comment on the keydates bot PR.

Reads the comment ONLY from env (never shell-interpolated — comment bodies on
a public repo are attacker-controlled), parses it against a strict grammar,
and appends the rejection to .github/keydates_rejections.json. The keydates worker
reads that file each run and never re-proposes a matching date.

Syntax:  /reject <event_id> <category>.<kind> <date> — <reason...>
Example: /reject anthrocon-2026 registration.closes 2026-06-26 — that's the pre-reg deadline
"""
import datetime
import json
import os
import random
import re
import subprocess
import sys
import time

# (?!\S) forces the date to end at whitespace/end-of-comment: without it,
# a typo like 2026-07-125 matched as date 2026-07-12 with the stray "5"
# swallowed into the reason.
GRAMMAR = re.compile(
    r"^/reject\s+([a-z0-9-]+)\s+"
    r"(registration|hotel|dealers|panels|performances|djs|volunteers)\.(opens|closes)\s+"
    r"(\d{4}-\d{2}-\d{2})(?!\S)\s*(?:[—–-]+\s*)?(.*)$",
    re.S,
)

REJECTIONS_FILE = ".github/keydates_rejections.json"

def parse(body):
    """Parse a /reject comment. Returns (fields, None) or (None, error)."""
    m = GRAMMAR.match(body.strip())
    if not m:
        return None, "parse-failure"
    try:
        datetime.date.fromisoformat(m.group(4))
    except ValueError:
        return None, "invalid-date"
    return m.groups(), None

def main() -> int:
    body = os.environ.get("COMMENT_BODY", "")
    user = os.environ.get("COMMENT_USER", "")
    created = os.environ.get("COMMENT_CREATED_AT", "")

    fields, err = parse(body)
    if err:
        print(err, end="")
        return 0  # workflow reacts with 👎 based on the output

    event_id, category, kind, date, reason = fields
    reason = " ".join(reason.split())[:300]  # collapse whitespace, cap length

    entry = {
        "event_id": event_id,
        "category": category,
        "kind": kind,
        "date": date,
        "reason": reason,
        "by": user,
        "at": created,
    }

    # our stdout is captured into $GITHUB_OUTPUT — only the sentinel word may
    # reach it, so route every git subprocess's stdout to stderr
    def git(*args, check=True):
        return subprocess.run(["git", *args], check=check, stdout=sys.stderr)

    key = ("event_id", "category", "kind", "date")
    # Batched /reject comments now run in parallel (one concurrency group per
    # comment id), so serialize at the git layer: on every attempt re-sync to
    # the latest origin/main, then dedup + append against THAT and push. We
    # RECOMPUTE the append each time rather than rebasing our own commit —
    # two runs appending to the same JSON array would otherwise conflict on
    # rebase or clobber each other's entry.
    # Budget must cover the worst case where N racing runners each need their
    # own attempt to win the push; 25 is generous headroom over observed batch
    # sizes. A transient git error (fetch/reset/add/commit) is caught and
    # counted as a failed attempt rather than crashing without a sentinel.
    for attempt in range(25):
        try:
            git("config", "user.name", "cons.fyi GitHub bot")
            git("config", "user.email", "github@cons.fyi")
            git("fetch", "origin", "main")
            git("reset", "--hard", "origin/main")
            with open(REJECTIONS_FILE) as f:
                rejections = json.load(f)
            if any(all(r.get(k) == entry[k] for k in key) for r in rejections):
                print("duplicate", end="")
                return 0
            rejections.append(entry)
            with open(REJECTIONS_FILE, "w") as f:
                json.dump(rejections, f, indent=2, ensure_ascii=False)
                f.write("\n")
            git("add", REJECTIONS_FILE)
            git("commit", "-m", f"Reject key date {event_id} {category}.{kind} {date}")
            if git("push", check=False).returncode == 0:
                print("ok", end="")
                return 0
        except (subprocess.CalledProcessError, OSError, json.JSONDecodeError) as e:
            # stdout is the sentinel channel, so log the swallowed error to
            # stderr — an exhausted loop is then diagnosable (which step, why).
            # A corrupt/missing rejections file loops to push-failure so the
            # commenter still gets a 👎 rather than a silent crash.
            print(f"reject attempt {attempt} failed: {e!r}", file=sys.stderr)
        # jitter so near-simultaneous losers don't retry in lockstep
        # (skip after the final attempt — no point sleeping before we give up)
        if attempt < 24:
            time.sleep(0.5 * (attempt + 1) + random.uniform(0, 0.5))
    # Exhausted: emit the sentinel but exit 0 so the Record step succeeds and
    # the React step still runs to post feedback (exit 1 would skip it, which
    # is the silent-drop bug this loop fixes).
    print("push-failure", end="")
    return 0

if __name__ == "__main__":
    sys.exit(main())
