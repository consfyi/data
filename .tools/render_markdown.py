#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "mistune",
# ]
# ///

import mistune
import sys


class TitleExtractingRenderer(mistune.HTMLRenderer):
    def __init__(self):
        super().__init__()
        self.title = None

    def heading(self, text, level):
        if level == 1 and self.title is None:
            self.title = text
        return super().heading(text, level)


renderer = TitleExtractingRenderer()
markdown = mistune.create_markdown(renderer=renderer)
html = markdown(sys.stdin.read())

print(
    f"""<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <meta http-equiv="X-UA-Compatible" content="IE=Edge">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@lowlighter/matcha@3.0.0/dist/matcha.min.css">
    <title>{renderer.title}</title>
</head>
<body>
{html}
</body>
</html>
"""
)
