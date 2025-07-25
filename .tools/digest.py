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


active = []
for con in cons:
    if pred(con):
        active.append(con)
    else:
        id = con["id"]
        os.rename(f"{id}.toml", f"archived/{id}.toml")

active.sort(
    key=lambda con: (
        whenever.Date.parse_common_iso(con["startDate"]),
        whenever.Date.parse_common_iso(con["endDate"]),
        con["id"],
    )
)


with open(OUTPUT_DIR / "calendar.ics", "w") as f:
    f.write("BEGIN:VCALENDAR\n")
    f.write("VERSION:2.0\n")
    f.write("PRODID:-//cons.fyi//EN\n")
    f.write("X-WR-CALNAME:cons.fyi\n")
    for con in active:
        timezone = con.get("timezone", "Utc")
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
        f.write("BEGIN:VEVENT\n")
        f.write(f"UID:{con['id']}\n")
        f.write(f"SUMMARY:{con['name']}\n")
        f.write(f"DTSTART;TZID={timezone};VALUE=DATE:{start_date}\n")
        f.write(f"DTEND;TZID={timezone};VALUE=DATE:{end_date}\n")
        f.write(f"URL:{con['url']}\n")
        f.write(f"LOCATION:{con['address']}\n")
        f.write("END:VEVENT\n")
    f.write("END:VCALENDAR\n")

with open(OUTPUT_DIR / "active.json", "w") as f:
    json.dump(
        active,
        f,
        ensure_ascii=False,
    )

cons_path = OUTPUT_DIR / "cons"
shutil.rmtree(cons_path, ignore_errors=True)

os.mkdir(cons_path)

ids = []

for fn in os.listdir("archived"):
    id, ext = os.path.splitext(fn)
    if ext != ".json":
        continue
    ids.append(id)
    shutil.copy(os.path.join("archived", fn), cons_path / fn)

for fn in os.listdir("."):
    id, ext = os.path.splitext(fn)
    if ext != ".json":
        continue
    ids.append(id)
    shutil.copy(fn, cons_path / fn)

ids.sort()

with open(OUTPUT_DIR / "index.json", "w") as f:
    json.dump(
        ids,
        f,
        ensure_ascii=False,
    )
