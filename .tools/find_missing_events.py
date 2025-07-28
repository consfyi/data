#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
# ]
# ///

import datetime
import os
import json

now = datetime.date.today()

all_series = []

for fn in sorted(os.listdir(".")):
    series_id, ext = os.path.splitext(fn)
    if ext != ".json":
        continue

    with open(fn) as f:
        series = json.load(f)
        start = max(
            (
                datetime.date.fromisoformat(event["startDate"])
                for event in series["events"]
            ),
            default=None,
        )
        if start is None or start < now:
            all_series.append((start, series_id, series))

all_series.sort()
for date, id, series in all_series:
    print(date, id)
    print(series["events"][0]["url"])
    print()
