#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
# ]
# ///

import itertools
import json
import sys
import os


def reorder(obj, schema):
    if schema["type"] == "object":
        props = schema.get("properties", {})
        return {k: reorder(obj[k], props[k]) for k in props if k in obj} | {
            k: v for k, v in obj.items() if k not in props
        }
    elif schema["type"] == "array":
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
    else:
        return obj


with open(os.path.join(os.path.dirname(__file__), "schema.json")) as f:
    schema = json.load(f)


json.dump(
    reorder(json.load(sys.stdin), schema), sys.stdout, indent=2, ensure_ascii=False
)
sys.stdout.write("\n")
