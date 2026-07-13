#!/usr/bin/env python3
"""Handle a `/reject` comment on the keydates bot PR.

Reads the comment ONLY from env (never shell-interpolated — comment bodies on
a public repo are attacker-controlled), parses it against a strict grammar,
and appends the rejection to .github/keydates_rejections.json. The keydates worker
reads that file each run and never re-proposes a matching date.

Syntax:  /reject <event_id> <category>.<kind> <date> — <reason...>
Example: /reject anthrocon-2026 registration.closes 2026-06-26 — that's the pre-reg deadline
"""
import json
import os
import re
import subprocess
import sys

GRAMMAR = re.compile(
    r"^/reject\s+([a-z0-9-]+)\s+"
    r"(registration|hotel|dealers|panels|performances|djs|volunteers)\.(opens|closes)\s+"
    r"(\d{4}-\d{2}-\d{2})\s*(?:[—–-]+\s*)?(.*)$",
    re.S,
)

def main() -> int:
    body = os.environ.get("COMMENT_BODY", "")
    user = os.environ.get("COMMENT_USER", "")
    created = os.environ.get("COMMENT_CREATED_AT", "")

    m = GRAMMAR.match(body.strip())
    if not m:
        print("parse-failure", end="")
        return 0  # workflow reacts with 👎 based on the output

    event_id, category, kind, date, reason = m.groups()
    reason = " ".join(reason.split())[:300]  # collapse whitespace, cap length

    with open(".github/keydates_rejections.json") as f:
        rejections = json.load(f)

    entry = {
        "event_id": event_id,
        "category": category,
        "kind": kind,
        "date": date,
        "reason": reason,
        "by": user,
        "at": created,
    }
    if any(
        all(r.get(k) == entry[k] for k in ("event_id", "category", "kind", "date"))
        for r in rejections
    ):
        print("duplicate", end="")
        return 0

    rejections.append(entry)
    with open(".github/keydates_rejections.json", "w") as f:
        json.dump(rejections, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # our stdout is captured into $GITHUB_OUTPUT — only the sentinel word may
    # reach it, so route every git subprocess's stdout to stderr
    def git(*args, check=True):
        return subprocess.run(["git", *args], check=check, stdout=sys.stderr)

    git("config", "user.name", "cons.fyi GitHub bot")
    git("config", "user.email", "github@cons.fyi")
    git("add", ".github/keydates_rejections.json")
    git("commit", "-m", f"Reject key date {event_id} {category}.{kind} {date}")
    # two racing /reject comments: rebase and retry the push
    for _ in range(3):
        if git("push", check=False).returncode == 0:
            print("ok", end="")
            return 0
        git("pull", "--rebase")
    print("push-failure", end="")
    return 1

if __name__ == "__main__":
    sys.exit(main())
