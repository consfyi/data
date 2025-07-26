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
import shutil
import tzfpy
import whenever

OUTPUT_DIR = pathlib.Path(os.environ["OUTPUT_DIR"])

cons = []


for fn in os.listdir("."):
    id, ext = os.path.splitext(fn)
    if ext != ".json":
        continue
    with open(fn) as f:
        con = json.load(f)
    if "latLng" in con:
        (lat, lng) = con["latLng"]
        con["timezone"] = tzfpy.get_tz(lng, lat)
    cons.append({"id": id, **con})

now = whenever.Instant.now()


def pred(con):
    end_date = (
        whenever.Date.parse_common_iso(con["endDate"])
        .add(days=1)
        .at(whenever.Time(12, 0))
        .assume_tz(con.get("timezone", "Utc"))
    )
    return now < end_date.add(days=7) and not con.get("canceled", False)


active = [con for con in cons if pred(con)]

active.sort(
    key=lambda con: (
        whenever.Date.parse_common_iso(con["startDate"]),
        whenever.Date.parse_common_iso(con["endDate"]),
        con["id"],
    )
)

now = whenever.Instant.now().to_tz("UTC")

with open(OUTPUT_DIR / "calendar.ics", "w") as f:
    f.write("BEGIN:VCALENDAR\r\n")
    f.write("VERSION:2.0\r\n")
    f.write("PRODID:-//cons.fyi//EN\r\n")
    f.write("X-WR-CALNAME:cons.fyi\r\n")
    for con in active:
        start_date = (
            whenever.Date.parse_common_iso(con["startDate"])
            .py_date()
            .strftime("%Y%m%d")
        )
        end_date = (
            whenever.Date.parse_common_iso(con["endDate"])
            .add(days=1)
            .py_date()
            .strftime("%Y%m%d")
        )
        dtstamp = now.py_datetime().strftime("%Y%m%dT%H%M%SZ")
        f.write("BEGIN:VEVENT\r\n")
        f.write(f"UID:{con['id']}\r\n")
        f.write(f"SUMMARY:{con['name']}\r\n")
        f.write(f"DTSTART;VALUE=DATE:{start_date}\r\n")
        f.write(f"DTEND;VALUE=DATE:{end_date}\r\n")
        f.write(f"DTSTAMP:{dtstamp}\r\n")
        f.write(f"URL:{con['url']}\r\n")
        f.write(f"LOCATION:{con['location']}\r\n")
        f.write("END:VEVENT\r\n")
    f.write("END:VCALENDAR\r\n")

with open(OUTPUT_DIR / "active.json", "w") as f:
    json.dump(
        active,
        f,
        ensure_ascii=False,
    )

cons_path = OUTPUT_DIR / "cons"
shutil.rmtree(cons_path, ignore_errors=True)

os.mkdir(cons_path)

index = []


for fn in os.listdir("."):
    id, ext = os.path.splitext(fn)
    if ext != ".json":
        continue
    with open(fn) as f:
        con = json.load(f)
    with open(cons_path / fn, "w") as f:
        json.dump(con, f, indent=2, ensure_ascii=False)
    index.append({"id": id, "con": con["name"]})

index.sort(key=lambda con: con["id"])

with open(OUTPUT_DIR / "index.json", "w") as f:
    json.dump(
        index,
        f,
        ensure_ascii=False,
    )
