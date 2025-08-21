#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "termcolor",
#   "eviltransform",
#   "googlemaps",
#   "PyICU",
#   "regex",
#   "safer",
# ]
# ///

import termcolor
import eviltransform
import googlemaps
import datetime
import os
import math
import json
import uuid
import regex
import icu
import safer
import unicodedata
import webbrowser
import typing


GOOGLE_MAPS_API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]


def guess_language_for_region(region_code: str) -> icu.Locale:
    return icu.Locale.createFromName(f"und_{region_code}").addLikelySubtags()


def prompt_for_venue(gmaps, venue, lang):
    session_token = str(uuid.uuid4())
    predictions = gmaps.places_autocomplete(
        venue, session_token=session_token, language=lang
    )

    address = None
    country = None
    lat_lng = None

    while True:
        termcolor.cprint(f"    0) ", "magenta", end="")
        termcolor.cprint("(no address)")
        for i, prediction in enumerate(predictions):
            termcolor.cprint(f"    {i + 1}) ", "magenta", end="")
            st = prediction["structured_formatting"]
            termcolor.cprint(
                ", ".join(
                    part
                    for part in [
                        st["main_text"],
                        st.get("secondary_text"),
                    ]
                    if part is not None
                )
            )
        termcolor.cprint(f"    #? ", "magenta", end="")
        try:
            choice = int(input(""))
        except ValueError:
            continue

        if choice == 0:
            break

        if choice - 1 > len(predictions):
            continue
        selected = predictions[choice - 1]

        st = selected["structured_formatting"]
        if "secondary_text" in st:
            address = st["secondary_text"]

        place = gmaps.place(
            selected["place_id"],
            session_token=session_token,
            fields=["name", "geometry/location", "address_component"],
            language=lang,
        )
        venue = place["result"]["name"]

        l = place["result"]["geometry"]["location"]
        lat_lng = (l["lat"], l["lng"])
        country = next(
            component["short_name"]
            for component in place["result"]["address_components"]
            if "country" in component["types"]
        )
        if country == "CN":
            lat, lng = lat_lng
            lat_lng = eviltransform.gcj2wgs(lat, lng)

        break

    return venue, address, country, lat_lng


def slugify(s: str, langid: icu.Locale) -> str:
    return "-".join(
        regex.sub(
            r"[^\p{L}\p{N}\s-]+",
            "",
            icu.CaseMap.toLower(
                langid, unicodedata.normalize("NFKC", s.replace("&", "and"))
            ),
        ).split()
    )


def get_week_of_month(date: datetime.date) -> int:
    first = datetime.date(date.year, date.month, 1)
    return int(math.ceil((date.day + first.weekday()) / 7))


def get_weekday_in_nth_week(
    year: int, month: int, weekday: int, week: int
) -> datetime.date:
    first = datetime.date(year, month, 1)
    return first + datetime.timedelta(days=-first.weekday() + weekday + (week - 1) * 7)


def add_year_same_weekday(date: datetime.date) -> datetime.date:
    return get_weekday_in_nth_week(
        date.year + 1,
        date.month,
        date.weekday(),
        get_week_of_month(date),
    )


MUTE_LIST = os.path.join(os.path.dirname(__file__), "update_wizard_mutes")


def read_mute_list(today) -> typing.Dict[str, datetime.date]:
    try:
        f = open(MUTE_LIST)
    except FileNotFoundError:
        return {}

    mutes: typing.Dict[str, datetime.date] = {}
    with f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            expiry, _, series_id = line.partition(" ")
            if not series_id:
                continue
            try:
                expiry = datetime.date.fromisoformat(expiry)
            except ValueError:
                continue
            if expiry < today:
                continue
            mutes[series_id] = max(mutes.setdefault(series_id, expiry), expiry)

    with safer.open(MUTE_LIST, "w") as f:
        for series_id, expiry in mutes.items():
            f.write(f"{expiry.isoformat()} {series_id}\n")

    return mutes


def add_mute_list_entry(series_id: str, expiry: datetime.date):
    with open(MUTE_LIST, "a") as f:
        f.write(f"{expiry.isoformat()} {series_id}\n")


def prompt_for_change(label, v=None):
    while True:
        if v is not None:
            termcolor.cprint(f"  {label}: ", "magenta", end="")
            termcolor.cprint(v, end="")
            termcolor.cprint("? ", "magenta", end="")
        else:
            termcolor.cprint(f"  {label}? ", "magenta", end="")
        inp = input().strip()
        if inp:
            v = inp
        if v is not None:
            break
    return v


def main():
    today = datetime.date.today()
    mutes = read_mute_list(today)

    gmaps = googlemaps.Client(key=GOOGLE_MAPS_API_KEY)

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
            if start is not None and start >= today or series_id in mutes:
                continue
            no_upcoming.append((start, series_id, series))
            for event in series["events"]:
                if "guessed" in event.get("sources", []):
                    guessed.append(event)

    no_upcoming.sort()

    termcolor.cprint(f"found {len(no_upcoming)} series to review", "cyan")
    padding = 0
    if no_upcoming:
        padding = math.ceil(math.log10(len(no_upcoming)))
        for i, (previous_start_date, series_id, _) in enumerate(no_upcoming):
            termcolor.cprint(f"{i+1:>{padding}}/{len(no_upcoming)} ", "cyan", end="")
            termcolor.cprint(f"{previous_start_date} ", "green", end="")
            termcolor.cprint(series_id, attrs=["bold"])
        print("")

    i = 0
    while True:
        if i < len(no_upcoming):
            previous_start_date, series_id, series = no_upcoming[i]
            previous_event = series["events"][0]

            termcolor.cprint(f"{i+1:>{padding}}/{len(no_upcoming)} ", "cyan", end="")
            termcolor.cprint(f"{previous_start_date} ", "green", end="")
            termcolor.cprint(series_id, attrs=["bold"])
            termcolor.cprint(previous_event["url"], "blue")

        while True:
            if i < len(no_upcoming):
                termcolor.cprint(
                    "(a)dd/(n)ew/(w)ebsite/(m)ute/(q)uit/(S)kip? ", "magenta", end=""
                )
            else:
                termcolor.cprint("(n)ew/(Q)uit? ", "magenta", end="")

            inp = input().strip().lower()
            if i < len(no_upcoming):
                match inp:
                    case "a":
                        handle_add(gmaps, series_id, series)
                        i += 1
                        break
                    case "w":
                        webbrowser.open(previous_event["url"])
                    case "m":
                        expiry = today + datetime.timedelta(days=90)
                        termcolor.cprint(
                            f"  adding to mute list, won't ask until {expiry}", "yellow"
                        )
                        add_mute_list_entry(series_id, expiry)
                        i += 1
                        break

            match inp:
                case "n":
                    handle_new(gmaps)
                    break
                case "s":
                    i += 1
                    break
                case "q":
                    return
                case "":
                    if i >= len(no_upcoming):
                        return
                    i += 1
                    break
                case x:
                    try:
                        x = int(x)
                    except ValueError:
                        continue
                    x -= 1
                    if 0 <= x < len(no_upcoming):
                        i = x
                        break
                    continue

        print("")


def handle_add(gmaps, series_id, series):
    previous_event = series["events"][0]

    previous_start_date = datetime.date.fromisoformat(previous_event["startDate"])
    previous_end_date = datetime.date.fromisoformat(previous_event["endDate"])

    event = {**previous_event}

    start_date = add_year_same_weekday(previous_start_date)
    while True:
        try:
            start_date = datetime.date.fromisoformat(
                prompt_for_change("start date", start_date.isoformat())
            )
        except ValueError:
            continue
        break
    event["startDate"] = start_date.isoformat()

    while True:
        try:
            end_date = datetime.date.fromisoformat(
                prompt_for_change(
                    "end date",
                    (
                        start_date + (previous_end_date - previous_start_date)
                    ).isoformat(),
                )
            )
        except ValueError:
            continue
        break
    event["endDate"] = end_date.isoformat()

    suffix = start_date.year
    _, previous_suffix = previous_event["name"].rsplit(" ", 1)
    try:
        previous_suffix = int(previous_suffix)
    except:
        pass
    else:
        suffix = previous_suffix + 1

    guessed_name = f"{series['name']} {suffix}"
    event["name"] = prompt_for_change("name", guessed_name)

    lang = (
        guess_language_for_region(event["country"])
        if "country" in event
        else icu.Locale.createFromName("en")
    )
    if event["name"] != guessed_name:
        event["id"] = slugify(event["name"], lang)
        event["id"] = prompt_for_change("event id", event["id"])
    else:
        event["id"] = f"{series_id}-{suffix}"

    event["venue"] = prompt_for_change("venue", event["venue"])
    if event["venue"] != previous_event["venue"]:
        venue, address, country, lat_lng = prompt_for_venue(gmaps, event["venue"], "en")
        event["venue"] = venue

        if address is not None:
            event["address"] = address
        else:
            del event["address"]

        if country is not None:
            event["country"] = country

        if lat_lng is not None:
            event["latLng"] = lat_lng
        else:
            del event["latLng"]

    fn = f"{series_id}.json"
    termcolor.cprint(f"  {fn} / {event['id']}", attrs=["bold"])
    print(
        "\n".join(
            f"  {l}"
            for l in json.dumps(event, indent=2, ensure_ascii=False).split("\n")
        )
    )

    series["events"].insert(0, event)
    with open(fn, "w") as f:
        json.dump(series, f, indent=2, ensure_ascii=False)
        f.write("\n")


def handle_new(gmaps):
    series_name = prompt_for_change("series name")
    url = prompt_for_change("website")
    venue = prompt_for_change("venue")
    venue, address, country, lat_lng = prompt_for_venue(gmaps, venue, "en")

    while True:
        try:
            start_date = datetime.date.fromisoformat(prompt_for_change("start date"))
        except ValueError:
            continue
        break

    while True:
        try:
            end_date = datetime.date.fromisoformat(prompt_for_change("end date"))
        except ValueError:
            continue
        if end_date < start_date:
            continue
        break

    series_id = slugify(
        series_name,
        guess_language_for_region(country)
        if country is not None
        else icu.Locale.createFromName("en"),
    )

    suffix = prompt_for_change("suffix", str(start_date.year))
    event_id = prompt_for_change("event id", f"{series_id}-{suffix}")

    series = {
        "name": series_name,
        "events": [
            {
                "id": event_id,
                "name": f"{series_name} {suffix}",
                "url": url,
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "venue": venue,
                **({"address": address} if address is not None else {}),
                **({"country": country} if country is not None else {}),
                **({"latLng": lat_lng} if lat_lng is not None else {}),
            },
        ],
    }

    fn = f"{series_id}.json"
    termcolor.cprint(f"  {fn}", attrs=["bold"])
    print(
        "\n".join(
            f"  {l}"
            for l in json.dumps(series, indent=2, ensure_ascii=False).split("\n")
        )
    )

    with open(fn, "w") as f:
        json.dump(series, f, indent=2, ensure_ascii=False)
        f.write("\n")


if __name__ == "__main__":
    main()
