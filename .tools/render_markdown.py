#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "mistune",
#   "pygments",
# ]
# ///

import mistune
import sys
from pygments import highlight
from pygments.lexers import get_lexer_by_name
from pygments.formatters import html


class MyRenderer(mistune.HTMLRenderer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.title = None

    def heading(self, text, level):
        if level == 1 and self.title is None:
            self.title = text
        return super().heading(text, level)

    def block_code(self, code, info=None):
        if info is not None:
            lexer = get_lexer_by_name(info, stripall=True)
            formatter = html.HtmlFormatter(cssclass="codehilite")
            return highlight(code, lexer, formatter)
        return "<pre><code>" + mistune.escape(code) + "</code></pre>"


renderer = MyRenderer()
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
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/pygments-css@1.0.0/friendly.css">
    <title>{renderer.title}</title>
</head>
<body>
{html}
</body>
</html>
"""
)
