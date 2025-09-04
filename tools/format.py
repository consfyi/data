#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "orjson"
# ]
# ///

import itertools
import orjson
import sys
import os


def reorder(obj, schema):
    match schema["type"]:
        case "object":
            props = schema.get("properties", {})
            return {k: reorder(obj[k], props[k]) for k in props if k in obj} | {
                k: v for k, v in obj.items() if k not in props
            }
        case "array":
            return [
                reorder(u, s)
                for u, s in zip(
                    obj,
                    itertools.chain(
                        schema.get("prefixItems", []),
                        itertools.cycle([schema["items"]]) if "items" in schema else [],
                    ),
                )
            ]
        case _:
            return obj


def main():
    with open(os.path.join(os.path.dirname(__file__), "schema.json"), "rb") as f:
        schema = orjson.loads(f.read())

    for fn in sys.argv[1:]:
        with open(fn, "r+b") as f:
            out = orjson.dumps(
                reorder(orjson.loads(f.read()), schema),
                option=orjson.OPT_INDENT_2 | orjson.OPT_APPEND_NEWLINE,
            )

            f.seek(0)
            f.truncate(0)

            f.write(out)


if __name__ == "__main__":
    main()
