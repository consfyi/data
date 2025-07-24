#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "toml",
#   "tzfpy[tzdata]",
#   "whenever",
# ]
# ///

import json
import os
import sys
import toml
import tzfpy
import whenever

cons = []

for fn in os.listdir("."):
    id, ext = os.path.splitext(fn)
    if ext != ".toml":
        continue
    with open(fn) as f:
        con = toml.load(f)
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
        .assume_tz(con["timezone"] if "timezone" in con else "Utc")
    )
    return now < end_date.add(days=7)


json.dump(
    sorted(
        [con for con in cons if pred(con)],
        key=lambda con: (whenever.Date.parse_common_iso(con["endDate"]), con["id"]),
    ),
    sys.stdout,
)
