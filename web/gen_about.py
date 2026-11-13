#!/usr/bin/env python3
"""Turn web/about.md into the HTML shown in the About dialog (dist/about.html).

A small Markdown subset, standard library only:
  # / ## / ### headings, paragraphs (blank-line separated), "- " / "* " and
  "1. " lists, **bold**, *italic*, `code`, [text](url) links, plain
  <a href="url">text</a> links, --- rules and <!-- comments --> (dropped).
  Everything else is escaped, so the file
  cannot inject markup.  Links open in a new tab; only http(s), mailto and
  relative URLs are allowed.

usage: gen_about.py about.md OUT.html
"""
import html
import re
import sys


def inline(text):
    # <a href="url">text</a> is the same as [text](url)
    text = re.sub(r'<a\s+href\s*=\s*"([^"\s]+)"\s*>(.*?)</a>', r"[\2](\1)", text, flags=re.I)
    text = html.escape(text, quote=True)
    codes = []

    def keep_code(m):                       # protect `code` from the rest
        codes.append("<code>%s</code>" % m.group(1))
        return "\0%d\0" % (len(codes) - 1)

    text = re.sub(r"`([^`]+)`", keep_code, text)

    def link(m):
        label, url = m.group(1), html.unescape(m.group(2)).strip()
        if not re.match(r"^(https?:|mailto:|[^:]*$)", url, re.I):
            return label
        return '<a href="%s" target="_blank" rel="noopener">%s</a>' % (html.escape(url, quote=True), label)

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", r"<em>\1</em>", text)
    return re.sub(r"\0(\d+)\0", lambda m: codes[int(m.group(1))], text)


def convert(md):
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    out, para, items, kind = [], [], [], None

    def flush():
        nonlocal para, items, kind
        if para:
            out.append("<p>%s</p>" % inline(" ".join(para)))
            para = []
        if items:
            out.append("<%s>%s</%s>" % (kind, "".join("<li>%s</li>" % inline(i) for i in items), kind))
            items, kind = [], None

    for raw in md.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        number = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if not stripped:
            flush()
        elif heading:
            flush()
            level = len(heading.group(1))
            out.append("<h%d>%s</h%d>" % (level, inline(heading.group(2)), level))
        elif re.match(r"^(-{3,}|\*{3,})$", stripped):
            flush()
            out.append("<hr>")
        elif bullet or number:
            want = "ul" if bullet else "ol"
            if para or (kind and kind != want):
                flush()
            kind = want
            items.append((bullet or number).group(1))
        elif items and raw[:1] in (" ", "\t"):  # continuation of a list item
            items[-1] += " " + stripped
        else:
            if items:
                flush()
            para.append(stripped)
    flush()
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as f:
        body = convert(f.read())
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        f.write(body)
