#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "tzfpy[tzdata]",
#   "whenever",
# ]
# ///

import json
import pathlib
import os
import tzfpy
import whenever

OUTPUT_DIR = pathlib.Path(os.environ["OUTPUT_DIR"])

cons_path = OUTPUT_DIR / "cons"
os.mkdir(cons_path)

index = {}
events = []


for fn in os.listdir("."):
    id, ext = os.path.splitext(fn)
    if ext != ".json":
        continue

    with open(fn) as f:
        con = json.load(f)

    with open(cons_path / fn, "w") as f:
        json.dump(con, f, indent=2, ensure_ascii=False)
    index[id] = {"name": con["name"]}

    for event in con["events"]:
        if "latLng" in event:
            (lat, lng) = event["latLng"]
            event["timezone"] = tzfpy.get_tz(lng, lat)
        events.append(event)


with open(OUTPUT_DIR / "index.json", "w") as f:
    json.dump(
        index,
        f,
        ensure_ascii=False,
    )

now = whenever.Instant.now()


def pred(event):
    end_date = (
        whenever.Date.parse_common_iso(event["endDate"])
        .add(days=1)
        .at(whenever.Time(12, 0))
        .assume_tz(event.get("timezone", "Utc"))
    )
    return now < end_date.add(days=7) and not event.get("canceled", False)


active = [event for event in events if pred(event)]

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


with open(OUTPUT_DIR / "calendar.ics", "w") as f:
    f.write("BEGIN:VCALENDAR\r\n")
    f.write("VERSION:2.0\r\n")
    f.write("PRODID:-//cons.fyi//EN\r\n")
    f.write("X-WR-CALNAME:cons.fyi\r\n")
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

with open(OUTPUT_DIR / "active.json", "w") as f:
    json.dump(
        active,
        f,
        ensure_ascii=False,
    )
