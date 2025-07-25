#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "atomicwrites",
#   "bs4",
#   "httpx",
#   "googlemaps",
#   "PyICU",
#   "regex",
# ]
# ///
import asyncio
import atomicwrites
from bs4 import BeautifulSoup
import datetime
import html
import httpx
import googlemaps
import json
import icu
import logging
import pathlib
import regex
import os
import typing
import unicodedata
import xml.etree.ElementTree as ET


logging.basicConfig(level=logging.INFO)


def guess_language_for_region(region_code: str) -> icu.Locale:
    return icu.Locale.createFromName(f"und_{region_code}").addLikelySubtags()


def to_lower(s: str, langid: icu.Locale) -> str:
    return icu.CaseMap.toLower(langid, unicodedata.normalize("NFKC", s))


def slugify(s: str, langid: icu.Locale) -> str:
    return "-".join(
        regex.sub(
            r"[^\p{L}\p{N}\s-]+", "", to_lower(s.replace("&", "and"), langid)
        ).split()
    )


async def fetch_bytes(client: httpx.AsyncClient, url: str) -> bytes:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.content


with open(os.path.join(os.path.dirname(__file__), "countries.json"), "rb") as f:
    COUNTRIES = json.load(f)


OUTPUT_DIR = pathlib.Path(os.environ.get("OUTPUT_DIR", "."))
CALENDAR_URL = os.environ.get("CALENDAR_URL", "https://furrycons.com/calendar/")
MAP_URL = os.environ.get(
    "MAP_URL", "https://furrycons.com/calendar/map/yc-maps/map-upcoming.xml"
)
GOOGLE_MAPS_API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]


async def fetch_map(
    client: httpx.AsyncClient, url: str
) -> dict[str, tuple[float, float]]:
    resp = await client.get(url)
    resp.raise_for_status()

    markers = {}
    for marker in ET.fromstring(resp.content).findall("marker"):
        markers[marker.attrib["id"]] = (
            float(marker.attrib["lat"]),
            float(marker.attrib["lng"]),
        )
    return markers


async def fetch_calendar(
    client: httpx.AsyncClient, url: str
) -> list[dict[str, typing.Any]]:
    events = []

    resp = await client.get(url)
    resp.raise_for_status()

    for script in BeautifulSoup(resp.content, "html.parser").find_all(
        "script", {"type": "application/ld+json"}
    ):
        entries = json.loads(html.unescape(script.string or "").replace("\n", " "))
        if not isinstance(entries, list):
            entries = [entries]

        for entry in entries:
            if (
                entry.get("@context") != "http://schema.org"
                or entry.get("@type") != "Event"
            ):
                continue
            events.append(entry)

    return events


async def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    gmaps = googlemaps.Client(key=GOOGLE_MAPS_API_KEY)

    async with httpx.AsyncClient() as client:
        calendar, markers = await asyncio.gather(
            fetch_calendar(client, CALENDAR_URL),
            fetch_map(client, MAP_URL),
        )

        for entry in calendar:
            try:
                name = entry["name"]
                status = entry["eventStatus"]
                url = entry["url"]
                start_date = datetime.date.fromisoformat(entry["startDate"])
                end_date = datetime.date.fromisoformat(entry["endDate"])
                location = entry["location"]
                loc_name = location["name"]
                address = location["address"]
                country_name = address["addressCountry"]
                country_code = COUNTRIES.get(country_name)
                canceled = False
                if not country_code:
                    raise ValueError(f"Unknown country: {country_name}")

                event_id = slugify(name, guess_language_for_region(country_code))
                path = pathlib.Path(OUTPUT_DIR) / f"{event_id}.json"

                if status not in [
                    "https://schema.org/EventScheduled",
                    "https://schema.org/EventRescheduled",
                ]:
                    canceled = True
                    if path.exists():
                        with open(path, "r") as f:
                            con = json.load(f)
                        con["canceled"] = canceled
                        with atomicwrites.atomic_write(path, overwrite=True) as f:
                            json.dump(
                                con,
                                f,
                                ensure_ascii=False,
                                indent=2,
                            )
                        logging.info(f"Canceled: {path}")
                        continue

                if path.exists():
                    continue

                addr_parts = [
                    loc_name,
                    address.get("addressLocality", ""),
                    address.get("addressRegion", ""),
                    country_name,
                ]
                full_address = ", ".join(part for part in addr_parts if part)

                match = regex.search(r"/event/(\d+)/", url)
                assert match is not None
                fc_id = match.group(1)
                lat_lng = markers.get(fc_id) if fc_id else None

                if not lat_lng:
                    logging.warning(f"No marker found, geocoding: {event_id}")
                    geocode = gmaps.geocode(full_address)
                    if geocode:
                        location = geocode[0]["geometry"]["location"]
                        lat_lng = [location["lat"], location["lng"]]

                event_data = {
                    "name": name,
                    "url": url,
                    "startDate": start_date.isoformat(),
                    "endDate": end_date.isoformat(),
                    "location": full_address,
                    "country": country_code,
                    "latLng": lat_lng,
                    "source": "fancons.com",
                }
                if canceled:
                    event_data["canceled"] = True

                with atomicwrites.atomic_write(path, overwrite=True) as f:
                    json.dump(
                        event_data,
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )

                logging.info(f"Added: {path}")

            except Exception as e:
                logging.warning(f"Failed to process event: {e}")


if __name__ == "__main__":
    asyncio.run(main())
