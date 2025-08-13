#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "tabulate"
# ]
# ///

import datetime
import os
import json
import tabulate


def main():
    now = datetime.date.today()

    guessed = []
    no_upcoming = []

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
                no_upcoming.append((start, series_id, series))
            for event in series["events"]:
                if "guessed" in event.get("sources", []):
                    guessed.append(event)

    no_upcoming.sort()

    print(
        tabulate.tabulate(
            (
                (date, id, series["events"][0]["url"])
                for date, id, series in no_upcoming
            ),
            headers=["date", "id", "url"],
        )
    )
    print("")

    guessed.sort(key=lambda event: event["startDate"])
    print(
        tabulate.tabulate(
            ((event["startDate"], event["id"], event["url"]) for event in guessed),
            headers=["date", "id", "url"],
        )
    )


if __name__ == "__main__":
    main()
