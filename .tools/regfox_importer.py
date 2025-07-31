#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "bs4",
#   "httpx",
#   "dukpy",
#   "whenever",
# ]
# ///

import dukpy
import sys
from bs4 import BeautifulSoup
import json
import logging
import httpx
import os
import whenever

logging.basicConfig(level=logging.INFO)


def main():
    _, fn, regfox_url = sys.argv

    series_id, ext = os.path.splitext(fn)

    with open(fn) as f:
        series = json.load(f)

    resp = httpx.get(regfox_url)
    resp.raise_for_status()

    interpreter = dukpy.JSInterpreter()
    interpreter.evaljs("var window = {}")

    for script in BeautifulSoup(resp.content, "html.parser").find_all("script"):
        try:
            interpreter.evaljs(script.text)
        except:
            pass

    app_settings = json.loads(interpreter.evaljs("window.__BOOTSTRAP__.appSettings"))

    start_date = whenever.OffsetDateTime.parse_common_iso(
        app_settings["calendarInfo"]["date"]
    ).date()
    end_date = whenever.OffsetDateTime.parse_common_iso(
        app_settings["calendarInfo"]["endDate"]
    ).date()

    id = f"{series_id}-{start_date.year}"

    for i, e in enumerate(series["events"]):
        if whenever.Date.parse_common_iso(e["startDate"]) <= start_date:
            break
    else:
        i = len(series["events"])

    previous_event = None
    if i < len(series["events"]):
        previous_event = series["events"][i]
        if previous_event["id"] == id:
            return

    if previous_event is None:
        return

    event = {
        "id": id,
        "name": f"{series['name']} {start_date.year}",
        "url": previous_event["url"],
        "startDate": start_date.format_common_iso(),
        "endDate": end_date.format_common_iso(),
        "venue": previous_event["venue"],
        "address": previous_event.get("address"),
        "country": previous_event.get("country"),
        "latLng": previous_event.get("latLng"),
    }
    logging.info(f"imported: {event}")
    series["events"].insert(i, {k: v for k, v in event.items() if v is not None})

    with open(fn, "w") as f:
        json.dump(series, f, indent=2, ensure_ascii=False)
        f.write("\n")


main()
