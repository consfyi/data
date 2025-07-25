#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "httpx",
# ]
# ///
import httpx
import json
import sys

countries = {}
for country in (
    httpx.get("https://restcountries.com/v3.1/all?fields=name,altSpellings,cca2")
    .raise_for_status()
    .json()
):
    cca2 = country["cca2"]
    for name in sorted(
        [
            country["name"]["common"],
            country["name"]["official"],
            *country["altSpellings"],
        ]
    ):
        countries[name] = cca2

json.dump(countries, sys.stdout, ensure_ascii=False)
