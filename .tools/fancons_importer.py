#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "bs4",
#   "httpx",
#   "googlemaps",
#   "PyICU",
#   "regex",
# ]
# ///
import asyncio
import bisect
from bs4 import BeautifulSoup
import dataclasses
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
CALENDAR_URL = os.environ.get(
    "CALENDAR_URL", "https://furrycons.com/calendar/calendar.php"
)
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


@dataclasses.dataclass
class Event:
    con_id: str
    con_name: str
    id: str
    name: str
    url: str
    start_date: datetime.date
    end_date: datetime.date
    location: str
    country: str
    canceled: bool
    lat_lng: tuple[float, float] | None

    def geocode_lat_lng(self, gmaps: googlemaps.Client):
        if self.lat_lng is not None:
            return

        geocode = gmaps.geocode(self.location)
        if not geocode:
            return

        location = geocode[0]["geometry"]["location"]
        self.lat_lng = (location["lat"], location["lng"])

    def materialize_entry(self, gmaps: googlemaps.Client):
        self.geocode_lat_lng(gmaps)
        return {
            "id": self.id,
            "name": self.name,
            "startDate": self.start_date.isoformat(),
            "endDate": self.end_date.isoformat(),
            "location": self.location,
            "country": self.country,
            "latLng": self.lat_lng,
            "sources": ["fancons.com"],
            "url": self.url,
            **({"canceled": True} if self.canceled else {}),
        }


async def fetch_events():

    async with httpx.AsyncClient() as client:
        calendar, markers = await asyncio.gather(
            fetch_calendar(client, CALENDAR_URL),
            fetch_map(client, MAP_URL),
        )

        for entry in calendar:
            try:
                name = entry["name"]
                prefix, year = entry["name"].rsplit(" ", 1)

                url = entry["url"]
                start_date = datetime.date.fromisoformat(entry["startDate"])
                end_date = datetime.date.fromisoformat(entry["endDate"])
                loc = entry["location"]
                loc_name = loc["name"]
                address = loc["address"]
                country_name = loc["address"]["addressCountry"]
                country = COUNTRIES[country_name]
                location = ", ".join(
                    part
                    for part in [
                        loc_name,
                        address.get("addressLocality", ""),
                        address.get("addressRegion", ""),
                        country_name,
                    ]
                    if part
                )
                canceled = entry["eventStatus"] not in {
                    "https://schema.org/EventScheduled",
                    "https://schema.org/EventRescheduled",
                }

                lang = guess_language_for_region(country)
                con_id = slugify(prefix, lang)

                match = regex.search(r"/event/(\d+)/", url)
                assert match is not None
                fc_id = match.group(1)
                lat_lng = markers.get(fc_id) if fc_id else None

                yield Event(
                    con_id=con_id,
                    con_name=prefix,
                    id=f"{con_id}-{year}",
                    name=name,
                    url=url,
                    start_date=start_date,
                    end_date=end_date,
                    location=location,
                    country=country,
                    lat_lng=lat_lng,
                    canceled=canceled,
                )

            except Exception as e:
                logging.warning(f"Failed to process event: {e}")


async def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    gmaps = googlemaps.Client(key=GOOGLE_MAPS_API_KEY)
    async for event in fetch_events():
        fn = f"{event.con_id}.json"

        if os.path.exists(fn):
            with open(fn, "r") as f:
                con = json.load(f)
        else:
            fn = os.path.join("import_pending", fn)
            if os.path.exists(fn):
                with open(fn, "r") as f:
                    con = json.load(f)
            else:
                logging.info(f"Adding pending con {event.con_id}")
                con = {"name": event.con_name, "url": event.url, "events": []}

        if any(e["id"] == event.id for e in con["events"]):
            continue
        logging.info(f"Adding event {event.id} to {event.con_id}")

        if con["events"]:
            [*_, previous_event] = con["events"]
            event.url = previous_event["url"]

            # Handle numbered cons.
            previous_prefix, previous_suffix = previous_event["name"].rsplit(" ", 1)
            try:
                previous_suffix = int(previous_suffix)
            except:
                pass
            else:
                if (
                    datetime.date.fromisoformat(previous_event["startDate"]).year
                    != previous_suffix
                    or datetime.date.fromisoformat(previous_event["endDate"]).year
                    != previous_suffix
                ) and previous_prefix == con["name"]:
                    event.name = f"{con['name']} {previous_suffix + 1}"

        bisect.insort(
            con["events"],
            event.materialize_entry(gmaps),
            key=lambda event: (event["startDate"], event["endDate"]),
        )
        with open(fn, "w") as f:
            json.dump(con, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
