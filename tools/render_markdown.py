#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "mistune",
#   "pygments",
# ]
# ///

import html
import mistune
import sys
import re
from pygments import highlight
from pygments.lexers import get_lexer_by_name
from pygments.formatters import html as pygments_html


def slugify(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text


class MyRenderer(mistune.HTMLRenderer):
    title: str | None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.title = None

    def heading(self, text, level):
        if level == 1 and self.title is None:
            self.title = text
        return f'<h{level} id="{slugify(text)}">{text}</h{level}>'

    def block_code(self, code, info=None):
        if info is not None:
            lexer = get_lexer_by_name(info, stripall=True)
            formatter = pygments_html.HtmlFormatter(cssclass="codehilite")
            return highlight(code, lexer, formatter)
        return f"<pre><code>{mistune.escape(code)}</code></pre>"


def main():
    renderer = MyRenderer(escape=False)
    markdown = mistune.create_markdown(renderer=renderer)
    body = markdown(sys.stdin.read())

    print(
        f"""<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <meta http-equiv="X-UA-Compatible" content="IE=Edge">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@lowlighter/matcha@3.0.0/dist/matcha.lite.min.css">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/pygments-css@1.0.0/pastie.css">
    <title>{html.escape(renderer.title) if renderer.title is not None else ''}</title>
</head>
<body>
    {body}
</body>
</html>
"""
    )


if __name__ == "__main__":
    main()
