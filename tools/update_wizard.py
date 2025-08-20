#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "termcolor",
#   "eviltransform",
#   "googlemaps",
#   "PyICU",
#   "regex",
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
import unicodedata


GOOGLE_MAPS_API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]


def guess_language_for_region(region_code: str) -> icu.Locale:
    return icu.Locale.createFromName(f"und_{region_code}").addLikelySubtags()


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


def prompt_for_change(label, v):
    termcolor.cprint(f"  {label}: ", "magenta", end="")
    termcolor.cprint(v, end="")
    termcolor.cprint("? ", "magenta", end="")
    inp = input().strip()
    if inp:
        v = inp
    return v


def main():
    now = datetime.date.today()
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
            if start is None or start < now:
                no_upcoming.append((start, series_id, series))
            for event in series["events"]:
                if "guessed" in event.get("sources", []):
                    guessed.append(event)

    no_upcoming.sort()

    termcolor.cprint(f"found {len(no_upcoming)} series to review", "cyan")
    print("")

    for i, (previous_start_date, series_id, series) in enumerate(no_upcoming):
        previous_event = series["events"][0]
        previous_end_date = datetime.date.fromisoformat(previous_event["endDate"])

        termcolor.cprint(f"{i+1}/{len(no_upcoming)} ", "cyan", end="")
        termcolor.cprint(f"{previous_start_date} ", "green", end="")
        termcolor.cprint(series_id, attrs=["bold"])
        termcolor.cprint(previous_event["url"], "blue")
        while True:
            termcolor.cprint("(a)dd/(S)kip? ", "magenta", end="")
            match input().strip().lower():
                case "a":
                    start_date = add_year_same_weekday(previous_start_date)
                    event = {**previous_event}

                    while True:
                        try:
                            start_date = datetime.date.fromisoformat(
                                prompt_for_change("start date", start_date.isoformat())
                            )
                        except ValueError:
                            continue
                        break

                    while True:
                        try:
                            end_date = datetime.date.fromisoformat(
                                prompt_for_change(
                                    "end date",
                                    (
                                        start_date
                                        + (previous_end_date - previous_start_date)
                                    ).isoformat(),
                                )
                            )
                        except ValueError:
                            continue
                        break

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

                    if event["name"] != guessed_name:
                        event["id"] = slugify(
                            event["name"],
                            guess_language_for_region(event["country"])
                            if "country" in event
                            else icu.Locale.createFromName("en"),
                        )
                        event["id"] = prompt_for_change("id", event["id"])
                    else:
                        event["id"] = f"{series_id}-{suffix}"

                    event["venue"] = prompt_for_change("venue", event["venue"])
                    if event["venue"] != previous_event["venue"]:
                        session_token = str(uuid.uuid4())
                        predictions = gmaps.places_autocomplete(event["venue"])
                        while True:
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
                                selected = predictions[int(input("")) - 1]
                            except (ValueError, IndexError):
                                continue

                            st = selected["structured_formatting"]
                            event["venue"] = st["main_text"]
                            if "secondary_text" in st:
                                event["address"] = st["secondary_text"]
                            else:
                                del event["address"]

                            place = gmaps.place(
                                selected["place_id"],
                                session_token=session_token,
                                fields=["geometry/location", "address_component"],
                            )

                            l = place["result"]["geometry"]["location"]
                            event["latLng"] = (l["lat"], l["lng"])
                            event["country"] = next(
                                component["short_name"]
                                for component in place["result"]["address_components"]
                                if "country" in component["types"]
                            )
                            if event["country"] == "CN":
                                lat, lng = event["latLng"]
                                event["latLng"] = eviltransform.gcj2wgs(lat, lng)
                            break

                    print(
                        "\n".join(
                            f"  {l}"
                            for l in json.dumps(
                                event, indent=2, ensure_ascii=False
                            ).split("\n")
                        )
                    )

                    series["events"].insert(0, event)
                    with open(f"{series_id}.json", "w") as f:
                        json.dump(series, f, indent=2, ensure_ascii=False)
                        f.write("\n")

                    break
                case "s" | "":
                    # termcolor.cprint(
                    #     "  adding to skip list, won't ask until {expiry}", "yellow"
                    # )
                    break
                case _:
                    continue
        print("")


if __name__ == "__main__":
    main()
