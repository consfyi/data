#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "jsonschema",
#   "regex-as-re-globally",
#   "tzfpy[tzdata]",
#   "whenever",
#   "langconv",
#   "regex"
# ]
# ///

import json
import jsonschema.validators
import io
import itertools
import logging
import pathlib
import regex
import sys
import os
import tzfpy
import whenever
from langconv.converter import LanguageConverter
from langconv.language import Language, get_data_file_path


lc_hant = LanguageConverter.from_language(
    Language.from_json_files(
        "zh-hant",
        [get_data_file_path("zh/hant.json")],
        ["zh-hant", "zh-TW"],
    )
)

lc_hans = LanguageConverter.from_language(
    Language.from_json_files(
        "zh-hans",
        [get_data_file_path("zh/hans.json")],
        ["zh-hans"],
    )
)


logging.basicConfig(level=logging.INFO)


class ErrorLogger:
    def __init__(self):
        self.ok = True

    def log(self, id, path, msg):
        logging.error("%s:%s:%s", id, path, msg)
        self.ok = False


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


def foldline(line, limit=75):
    buf = io.StringIO()
    n = 0
    for c in line:
        m = len(c.encode("utf-8"))
        if n + m > limit:
            buf.write("\r\n ")
            n = 1
        buf.write(c)
        n += m
    return buf.getvalue()


def main():
    el = ErrorLogger()

    now = whenever.Instant.now().to_tz("UTC")

    (_, output_dir) = sys.argv
    output_dir = pathlib.Path(output_dir)

    with open(output_dir / "timestamp", "w") as f:
        f.write(now.py_datetime().strftime("%Y-%m-%dT%H:%M:%S.%fZ"))

    with open(os.path.join(os.path.dirname(__file__), "schema.json")) as f:
        schema = json.load(f)

    series_path = output_dir / "series"
    os.mkdir(series_path)

    events_path = output_dir / "events"
    os.mkdir(events_path)

    all_series = {}
    events = {}

    jsonschema.validators.Draft202012Validator.check_schema(schema)
    validator = jsonschema.validators.Draft202012Validator(
        schema, format_checker=jsonschema.validators.Draft202012Validator.FORMAT_CHECKER
    )

    for fn in sorted(os.listdir(".")):
        series_id, ext = os.path.splitext(fn)
        if ext != ".json":
            continue

        with open(fn) as f:
            series = json.load(f)

        has_errors = False
        for error in validator.iter_errors(series):
            el.log(series_id, error.json_path, error.message)
        if has_errors:
            continue

        for event, previous_event in itertools.zip_longest(
            series["events"], series["events"][1:], fillvalue=None
        ):
            assert event is not None

            event_locale_is_zh = event["locale"][:3] == "zh-"
            tls = event.get("translations", {})

            if event_locale_is_zh or "zh-Hans" in tls or "zh-Hant" in tls:
                input_locale = {
                    "zh-TW": "zh-Hant",
                    "zh-HK": "zh-Hant",
                    "zh-MO": "zh-Hant",
                    "zh-CN": "zh-Hans",
                }[event["locale"]]

                output_locale = {
                    "zh-Hans": "zh-Hant",
                    "zh-Hant": "zh-Hans",
                }[input_locale]

                lc = {
                    "zh-Hans": lc_hans,
                    "zh-Hant": lc_hant,
                }[output_locale]

                input_tls = tls.get(input_locale, {})

                name = input_tls.get(
                    "name", event["name"] if event_locale_is_zh else None
                )
                venue = input_tls.get(
                    "venue", event["venue"] if event_locale_is_zh else None
                )
                address = input_tls.get(
                    "address", event["address"] if event_locale_is_zh else None
                )

                output_tls = {
                    **(
                        {"name": name}
                        if name is not None and regex.search(r"\p{sc=Han}", name)
                        else {}
                    ),
                    **(
                        {"venue": lc.convert(venue)}
                        if venue is not None and regex.search(r"\p{sc=Han}", venue)
                        else {}
                    ),
                    **(
                        {"address": lc.convert(address)}
                        if address is not None and regex.search(r"\p{sc=Han}", address)
                        else {}
                    ),
                }

                if output_tls:
                    event.setdefault("translations", {})[output_locale] = output_tls

            event_id = event["id"]
            if "latLng" in event:
                (lat, lng) = event["latLng"]
                event["timezone"] = tzfpy.get_tz(lng, lat)
            event["seriesId"] = series_id

            if previous_event is not None and "attendance" in previous_event:
                event["previousAttendance"] = previous_event["attendance"]

            if event_id in events:
                el.log(
                    f"{series_id}/{event_id}",
                    "$.id",
                    f"not unique across all series, last seen in {events[event_id]['seriesId']}",
                )
            events[event_id] = event

            with open(events_path / f"{event_id}.json", "w") as f:
                json.dump(event, f, indent=2, ensure_ascii=False)

        with open(series_path / fn, "w") as f:
            json.dump(series, f, indent=2, ensure_ascii=False)
        all_series[series_id] = series

    if not el.ok:
        sys.exit(1)

    with open(output_dir / "series.json", "w") as f:
        json.dump(sorted(list(all_series.keys())), f, ensure_ascii=False)

    with open(output_dir / "events.json", "w") as f:
        json.dump(list(events), f, ensure_ascii=False)

    now = whenever.Instant.now()

    def pred(event):
        end_time = (
            whenever.Date.parse_common_iso(event["endDate"])
            .add(days=1)
            .at(whenever.Time(12, 0))
            .assume_tz(event.get("timezone", "UTC"))
        )
        return now < end_time.add(days=7) and not event.get("canceled", False)

    current = [event for event in events.values() if pred(event)]
    current.sort(
        key=lambda event: (
            whenever.Date.parse_common_iso(event["startDate"]),
            whenever.Date.parse_common_iso(event["endDate"]),
            event["id"],
        )
    )

    with open(output_dir / "calendar.ics", "w") as f:

        def write_line(line):
            f.write(foldline(line))
            f.write("\r\n")

        write_line("BEGIN:VCALENDAR")
        write_line("VERSION:2.0")
        write_line("PRODID:-//cons.fyi//EN")
        write_line("X-WR-CALNAME:cons.fyi")
        for event in current:
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
            location = event["venue"]
            if "address" in event:
                location += f", {event['address']}"
            write_line("BEGIN:VEVENT")
            write_line(f"UID:{event['id']}")
            write_line(f"SUMMARY:{escape_ics(event['name'])}")
            write_line(f"DTSTART;VALUE=DATE:{start_date}")
            write_line(f"DTEND;VALUE=DATE:{end_date}")
            write_line(f"DTSTAMP:{dtstamp}")
            write_line(f"URL:{escape_ics(event['url'])}")
            write_line(f"LOCATION:{escape_ics(location)}")
            if "latLng" in event:
                lat, lng = event["latLng"]
                write_line(f"GEO:{lat:.6f};{lng:.6f}")
            write_line("END:VEVENT")
        write_line("END:VCALENDAR")

    with open(output_dir / "current.jsonl", "w") as f:
        for event in current:
            json.dump(event, f, ensure_ascii=False)
            f.write("\n")

    last = [
        event
        for event in (
            next(iter(series["events"]), None) for _, series in all_series.items()
        )
        if event is not None
    ]
    last.sort(
        key=lambda event: (
            whenever.Date.parse_common_iso(event["startDate"]),
            whenever.Date.parse_common_iso(event["endDate"]),
            event["id"],
        )
    )

    with open(output_dir / "last.jsonl", "w") as f:
        for event in last:
            json.dump(event, f, ensure_ascii=False)
            f.write("\n")


if __name__ == "__main__":
    main()
