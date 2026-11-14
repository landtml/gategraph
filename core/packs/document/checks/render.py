"""Render the unit's Markdown to build/document.html. Exit 0 or 1.

A small Markdown subset, standard library only: ATX headings, paragraphs,
bullet lists, fenced code, and inline code, bold and italic. Anything else is
passed through as escaped text. An unclosed code fence is a render failure.

Rule: document.render_failed.
"""
import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _doc  # noqa: E402

OUTPUT = Path("build") / "document.html"


def inline(text):
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)


def to_html(document):
    out = []
    paragraph = []
    items = []
    code = None
    fence_line = 0

    def close_paragraph():
        if paragraph:
            out.append("<p>" + inline(" ".join(paragraph)) + "</p>")
            paragraph.clear()

    def close_list():
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(item)}</li>" for item in items) + "</ul>")
            items.clear()

    for number, raw in document.body():
        line = raw.strip()
        if code is not None:
            if line.startswith(_doc.FENCE):
                out.append("<pre><code>" + html.escape("\n".join(code), quote=False) + "</code></pre>")
                code = None
            else:
                code.append(raw)
            continue
        if line.startswith(_doc.FENCE):
            close_paragraph()
            close_list()
            code, fence_line = [], number
            continue
        if not line:
            close_paragraph()
            close_list()
            continue
        heading = _doc.HEADING.match(line)
        if heading:
            close_paragraph()
            close_list()
            level = len(line) - len(line.lstrip("#"))
            out.append(f"<h{level}>{inline(heading.group(1))}</h{level}>")
            continue
        if line.startswith(("- ", "* ")):
            close_paragraph()
            items.append(line[2:])
            continue
        close_list()
        paragraph.append(line)
    if code is not None:
        raise _doc.Problem("document.render_failed",
                           f"code fence opened on line {fence_line} is never closed", line=fence_line)
    close_paragraph()
    close_list()
    title = html.escape(document.meta["title"], quote=True)
    return ("<!doctype html>\n<html><head><meta charset=\"utf-8\">"
            f"<title>{title}</title></head>\n<body>\n" + "\n".join(out) + "\n</body></html>\n")


def check(argv):
    unit = Path(_doc.unit_from_argv(argv))
    document = _doc.load(unit)
    rendered = to_html(document)
    target = unit / OUTPUT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    _doc.run(check)
