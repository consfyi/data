#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "httpx",
#   "googlemaps",
#   "whenever",
# ]
# ///

import sys
import json
import logging
import googlemaps
import httpx
import os
import uuid
import whenever

logging.basicConfig(level=logging.INFO)

GOOGLE_MAPS_API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]
gmaps = googlemaps.Client(key=GOOGLE_MAPS_API_KEY)

_, fn, concat_url = sys.argv

series_id, ext = os.path.splitext(fn)

with open(fn) as f:
    series = json.load(f)

    resp = httpx.get(f"{concat_url}/api/config")
    resp.raise_for_status()
    config = resp.json()
    for convention in config["conventions"]:
        start_date = whenever.OffsetDateTime.parse_common_iso(
            convention["startAt"]
        ).date()
        end_date = whenever.OffsetDateTime.parse_common_iso(convention["endAt"]).date()

        id = f"{series_id}-{start_date.year}"

        for i, e in enumerate(series["events"]):
            if whenever.Date.parse_common_iso(e["startDate"]) <= start_date:
                break
        else:
            i = len(series["events"])

        if i < len(series["events"]):
            previous_event = series["events"][i]
            if previous_event["id"] == id:
                continue

        venue = convention["venue"]
        country = config["organization"]["country"]

        session_token = str(uuid.uuid4())
        predictions = gmaps.places_autocomplete(
            f"{venue}, {country}", session_token=session_token
        )

        venue = convention["venue"]
        address = None

        if len(predictions) == 0:
            lat_lng = None
        else:
            prediction, *_ = predictions
            st = prediction["structured_formatting"]
            if "secondary_text" in st:
                address = st["secondary_text"]

            place = gmaps.place(
                prediction["place_id"],
                session_token=session_token,
                fields=["geometry/location"],
            )
            l = place["result"]["geometry"]["location"]
            lat_lng = [l["lat"], l["lng"]]

        event = {
            "id": id,
            "name": f"{series['name']} {start_date.year}",
            "url": series["events"][0]["url"],
            "startDate": start_date.format_common_iso(),
            "endDate": end_date.format_common_iso(),
            "venue": venue,
            "address": address,
            "country": country,
            "latLng": lat_lng,
        }
        logging.info(f"imported: {event}")
        series["events"].insert(i, {k: v for k, v in event.items() if v is not None})

with open(fn, "w") as f:
    json.dump(series, f, indent=2, ensure_ascii=False)
    f.write("\n")
