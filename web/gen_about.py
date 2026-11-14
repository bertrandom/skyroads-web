#!/usr/bin/env python3
"""Turn web/about.md into the HTML shown in the About dialog (dist/about.html).

Standard CommonMark, rendered with markdown-it-py. The Makefile runs it with
`uv run`, which installs that from web/pyproject.toml into web/.venv.
Raw HTML in the file is passed through as written. Links open in a new tab.

usage: gen_about.py about.md OUT.html [REVISION]

REVISION (the git revision built) is added as <p class="rev">, which the
page shows in the dialog's lower right corner.
"""
import html
import sys

try:
    from markdown_it import MarkdownIt
except ImportError:
    sys.exit("gen_about.py: needs markdown-it-py: run it with `uv run gen_about.py`")

md = MarkdownIt("commonmark")


def link_open(self, tokens, idx, options, env):
    tokens[idx].attrSet("target", "_blank")
    tokens[idx].attrSet("rel", "noopener")
    return self.renderToken(tokens, idx, options, env)


md.add_render_rule("link_open", link_open)

if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as f:
        body = md.render(f.read())
    if len(sys.argv) > 3 and sys.argv[3]:
        body += '<p class="rev">%s</p>\n' % html.escape(sys.argv[3])
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        f.write(body)
