#!/usr/bin/env python3
"""Handle a `/reject` comment on the keydates bot PR.

Reads the comment ONLY from env (never shell-interpolated — comment bodies on
a public repo are attacker-controlled), parses it against a strict grammar,
and appends the rejection to keydates_rejections.json. The keydates worker
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
    r"(registration|hotel|dealers|panels|volunteers)\.(opens|closes)\s+"
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

    with open("keydates_rejections.json") as f:
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
    with open("keydates_rejections.json", "w") as f:
        json.dump(rejections, f, indent=2, ensure_ascii=False)
        f.write("\n")

    subprocess.run(["git", "config", "user.name", "cons.fyi GitHub bot"], check=True)
    subprocess.run(["git", "config", "user.email", "github@cons.fyi"], check=True)
    subprocess.run(["git", "add", "keydates_rejections.json"], check=True)
    subprocess.run(
        ["git", "commit", "-m", f"Reject key date {event_id} {category}.{kind} {date}"],
        check=True,
    )
    # two racing /reject comments: rebase and retry the push
    for _ in range(3):
        if subprocess.run(["git", "push"], check=False).returncode == 0:
            print("ok", end="")
            return 0
        subprocess.run(["git", "pull", "--rebase"], check=True)
    print("push-failure", end="")
    return 1

if __name__ == "__main__":
    sys.exit(main())
