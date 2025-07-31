#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
# ]
# ///
import html
import sys
import os

def main():
    (_, src, path) = sys.argv
    files = sorted(os.listdir(src))

    parts = path.split('/')

    print(
        f"""<!doctype html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta http-equiv="X-UA-Compatible" content="IE=Edge">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@lowlighter/matcha@3.0.0/dist/matcha.lite.min.css">
        <title>{html.escape(path)}</title>
    </head>
    <body>
    <h1>{'/'.join(f"<a href=\"{"../" * (len(parts) - i - 1)}\">{html.escape(part)}</a>" if i < len(parts) - 1 else html.escape(part) for i, part in enumerate(parts))}</h1>
    <ul>
    {"\n".join(f"<li><a href=\"{html.escape(fn)}\">{html.escape(fn)}</a></li>" for fn in files)}
    </ul>
    </body>
    </html>
    """
    )


if __name__ == "__main__":
    main()
