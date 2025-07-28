#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
# ]
# ///

import datetime
import os
import json

now = datetime.date.today()

cons = []

for fn in sorted(os.listdir(".")):
    con_id, ext = os.path.splitext(fn)
    if ext != ".json":
        continue

    with open(fn) as f:
        con = json.load(f)
        start = max(
            (
                datetime.date.fromisoformat(event["startDate"])
                for event in con["events"]
            ),
            default=None,
        )
        if start is None or start < now:
            cons.append((start, con_id))

cons.sort()
for date, id in cons:
    print(date, id)
