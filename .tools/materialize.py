#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "jsonschema",
#   "tzfpy[tzdata]",
#   "whenever",
# ]
# ///

import json
import jsonschema.validators
import logging
import pathlib
import sys
import os
import tzfpy
import whenever

logging.basicConfig(level=logging.INFO)


(_, output_dir) = sys.argv
output_dir = pathlib.Path(output_dir)

with open(os.path.join(os.path.dirname(__file__), "schema.json")) as f:
    schema = json.load(f)


series_path = output_dir / "series"
os.mkdir(series_path)

events_path = output_dir / "events"
os.mkdir(events_path)

series_index = []
events = {}

jsonschema.validators.Draft202012Validator.check_schema(schema)
validator = jsonschema.validators.Draft202012Validator(
    schema, format_checker=jsonschema.validators.Draft202012Validator.FORMAT_CHECKER
)


class ErrorLogger:
    def __init__(self):
        self.ok = True

    def log(self, id, path, msg):
        logging.error("%s:%s:%s", id, path, msg)
        self.ok = False


el = ErrorLogger()


for fn in sorted(os.listdir(".")):
    series_id, ext = os.path.splitext(fn)
    if ext != ".json":
        continue

    with open(fn) as f:
        series = json.load(f)

    has_errors = False
    for error in validator.iter_errors(series):
        el.log(series_id, error.json_path, error.message)
    if has_errors:
        continue

    for event in series["events"]:
        event_id = event["id"]
        if "latLng" in event:
            (lat, lng) = event["latLng"]
            event["timezone"] = tzfpy.get_tz(lng, lat)
        event["seriesId"] = series_id

        if event_id in events:
            el.log(
                f"{series_id}/{event_id}",
                "$.id",
                f"not unique across all series, last seen in {events[event_id]['seriesId']}",
            )
        events[event_id] = event

        with open(events_path / f"{event_id}.json", "w") as f:
            json.dump(event, f, indent=2, ensure_ascii=False)

    with open(series_path / fn, "w") as f:
        json.dump(series, f, indent=2, ensure_ascii=False)
    series_index.append(series_id)


if not el.ok:
    sys.exit(1)

with open(output_dir / "series.json", "w") as f:
    json.dump(
        series_index,
        f,
        ensure_ascii=False,
    )

with open(output_dir / "events.json", "w") as f:
    json.dump(
        list(events),
        f,
        ensure_ascii=False,
    )


now = whenever.Instant.now()


def pred(event):
    end_date = (
        whenever.Date.parse_common_iso(event["endDate"])
        .add(days=1)
        .at(whenever.Time(12, 0))
        .assume_tz(event.get("timezone", "UTC"))
    )
    return now < end_date.add(days=7) and not event.get("canceled", False)


active = [event for event in events.values() if pred(event)]

active.sort(
    key=lambda event: (
        whenever.Date.parse_common_iso(event["startDate"]),
        whenever.Date.parse_common_iso(event["endDate"]),
        event["id"],
    )
)

now = whenever.Instant.now().to_tz("UTC")


def escape_ics(s):
    return s.translate(
        str.maketrans(
            {
                "\\": "\\\\",
                "\n": r"\n",
                ",": r"\,",
                ";": r"\;",
            }
        )
    )


with open(output_dir / "calendar.ics", "w") as f:
    f.write("BEGIN:VCALENDAR\r\n")
    f.write("VERSION:2.0\r\n")
    f.write("PRODID:-//cons.fyi//EN\r\n")
    f.write("X-WR-CALNAME:series.fyi\r\n")
    for event in active:
        start_date = (
            whenever.Date.parse_common_iso(event["startDate"])
            .py_date()
            .strftime("%Y%m%d")
        )
        end_date = (
            whenever.Date.parse_common_iso(event["endDate"])
            .add(days=1)
            .py_date()
            .strftime("%Y%m%d")
        )
        dtstamp = now.py_datetime().strftime("%Y%m%dT%H%M%SZ")
        f.write("BEGIN:VEVENT\r\n")
        f.write(f"UID:{event['id']}\r\n")
        f.write(f"SUMMARY:{escape_ics(event['name'])}\r\n")
        f.write(f"DTSTART;VALUE=DATE:{start_date}\r\n")
        f.write(f"DTEND;VALUE=DATE:{end_date}\r\n")
        f.write(f"DTSTAMP:{dtstamp}\r\n")
        f.write(f"URL:{escape_ics(event['url'])}\r\n")
        f.write(f"LOCATION:{escape_ics(event['location'])}\r\n")
        f.write("END:VEVENT\r\n")
    f.write("END:VCALENDAR\r\n")

with open(output_dir / "active.json", "w") as f:
    json.dump(
        active,
        f,
        ensure_ascii=False,
    )
