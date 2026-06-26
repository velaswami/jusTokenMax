"""HTML -> Markdown extraction.

A web page is mostly non-content: scripts, styles, navigation chrome, and markup
attributes dwarf the actual prose. So unlike DOCX/PPTX (conversion), HTML genuinely
COMPRESSES — we drop the noise and keep the skeleton (heading hierarchy, lists,
tables, code, links) as Markdown, typically shrinking a real page 80-95%.

Built on the standard library's `html.parser` — NO third-party dependency, so it
keeps jusTokenMax's zero-dependency/auditable promise (a heuristic main-content
*scorer* like readability would be both a dependency and a non-deterministic
black box, the opposite of that promise). `html.parser` is lenient, so malformed
markup is tolerated rather than fatal; the only fail-open case is "not HTML".

Boilerplate removal is deliberately tag-based (drop nav/header/footer/aside),
never content-scored — so the main content tree is always preserved. Pages built
as div-soup with no semantic tags keep more chrome (a stated limitation).

Inline <svg> diagrams are dropped but flagged (like images), and JS-rendered
pages can't be parsed statically — when extraction yields almost nothing the
dispatch fails open and leaves the original (see HTML_MIN_YIELD_TOKENS), so the
agent can still read the raw page rather than getting an empty digest.

Known limitations (degrade gracefully, never crash):
  * Table colspan/rowspan are ignored — spanning cells pad with blanks rather
    than truly span, so wide spans can look ragged.
  * A table's first row is treated as the header even without <th>.
  * <meta charset> is not honored; input is read as UTF-8.
  * JavaScript-rendered content is invisible (static HTML only) — detected via
    the low-yield guard and left as-is, never rendered (no browser engine).
  * Inline SVG text (e.g. flowchart labels) is flagged, not extracted.
  * A non-page fragment that happens to start with `<html` (e.g. a template
    snippet saved as .txt) can be sniffed as HTML.
"""

from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser
from typing import List, Tuple

from .svg import _f as _svg_f, render as _render_svg

# Safety cap, same rationale as the other handlers — the Read hook runs this on
# untrusted input, so bound the work and the disk write.
MAX_OUTPUT_CHARS = 5_000_000

_TRUNCATED = "\n\n> _[justokenmax: output truncated — document exceeds safety caps]_\n"

# Tags whose entire subtree is non-content and dropped. (svg is handled
# specially — its text/draw.io-source is extracted, not dropped.)
_DROP = {"script", "style", "noscript", "template", "form", "head"}
# Navigation chrome dropped by default (tag-based, never content-scored).
_BOILER = {"nav", "header", "footer", "aside"}
_HEADINGS = {"h1": "# ", "h2": "## ", "h3": "### ",
             "h4": "#### ", "h5": "##### ", "h6": "###### "}
# Block-level tags that force a paragraph break (so adjacent content can't glue).
_BLOCK = {"p", "div", "section", "article", "main", "tbody", "thead",
          "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
          "dl", "dt", "dd", "figure", "figcaption", "caption"}
# Blocks that own a line prefix (heading hashes / list bullet / quote marker);
# their prefix must be cleared when they close so an empty one can't leak it on.
_PREFIX_OWNERS = set(_HEADINGS) | {"li", "blockquote"}

_WS = re.compile(r"\s+")


class _Extractor(HTMLParser):
    def __init__(self):
        # convert_charrefs=True: entity references in text are unescaped for us.
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.cur: List[str] = []        # active inline buffer
        self.block_prefix = ""          # heading hashes / list indent+bullet / "> "
        self.title = ""
        self.in_title = False
        self.skip = 0                   # depth inside a dropped/boilerplate subtree
        self.bold = 0
        self.italic = 0
        self.in_pre = False
        self.pre_buf: List[str] = []    # raw text inside <pre> (whitespace kept)
        self.list_depth = 0
        self.images = 0
        self.svgs = 0                   # textless SVGs (dropped, flagged)
        self.tables = 0
        self.saw_tag = False
        # inline-SVG capture (text/draw.io source extracted via svg.py)
        self.in_svg = 0
        self._svg_content = None
        self._svg_labels: List[tuple] = []
        self._svg_in_text = False
        self._svg_buf: List[str] = []
        self._svg_xy = (0.0, 0.0)
        self._href = ""
        # table state — current rows/row, with a stack for nested tables and a
        # stack of (inline buffer, prefix) for the cell currently being filled.
        self.rows: List[List[str]] = []
        self.row: List[str] = []
        self._table_stack: List[tuple] = []
        self._cell_stack: List[tuple] = []

    # -- helpers --
    def _flush(self):
        # Inside a table cell, a block boundary is just a separator — content
        # stays in the cell buffer and must never escape to self.out.
        if self._cell_stack:
            if self.cur and not self.cur[-1].endswith(" "):
                self.cur.append(" ")
            self.bold = self.italic = 0
            return
        # Strip/collapse the inline content; the block prefix (heading hashes,
        # list indent + bullet, quote marker) is kept so a nested block inside a
        # list item / blockquote / heading inherits it — but only cleared once it
        # has actually been emitted, so it can't be lost or leak to a later block.
        s = _WS.sub(" ", "".join(self.cur).strip())
        self.cur = []
        if s:
            self.out.append(self.block_prefix + s)
            self.block_prefix = ""
        self.bold = self.italic = 0     # emphasis never spans a block boundary

    def _drop(self, tag):
        return tag in _DROP or tag in _BOILER

    def _emit_block(self, md):
        """Place an already-rendered block (e.g. extracted SVG) into the output —
        inline if we're inside a table cell, else as its own block."""
        md = md.strip()
        if not md:
            return
        if self._cell_stack:
            if self.cur and not self.cur[-1].endswith(" "):
                self.cur.append(" ")
            self.cur.append(md.replace("\n", " "))
        else:
            self._flush()
            self.out.append(md)

    # -- tag handlers --
    def handle_starttag(self, tag, attrs):
        self.saw_tag = True
        if tag == "title" and not self.in_svg:
            self.in_title = True
            return
        if tag == "svg" and not self.skip:
            self.in_svg += 1                    # capture this subtree, don't drop
            if self.in_svg == 1:
                self._svg_content = dict(attrs).get("content")
                self._svg_labels = []
            return
        if self.in_svg:                         # inside a captured SVG
            if tag == "text":
                self._svg_in_text = True
                self._svg_buf = []
                a = dict(attrs)
                self._svg_xy = (_svg_f(a.get("y")), _svg_f(a.get("x")))
            return                              # ignore all other SVG geometry
        if self._drop(tag):
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "img":
            self.images += 1
            return
        if tag == "br":
            self.cur.append(" ")
        elif tag in _HEADINGS:
            self._flush()
            self.block_prefix = _HEADINGS[tag]
        elif tag in ("ul", "ol"):
            self.list_depth += 1
        elif tag == "li":
            self._flush()
            self.block_prefix = "  " * max(0, self.list_depth - 1) + "- "
        elif tag == "a":
            self._href = _html.unescape(dict(attrs).get("href", "") or "")
            if self._href:
                self.cur.append("[")    # no href -> render the text plainly
        elif tag in ("strong", "b"):
            self.bold += 1
        elif tag in ("em", "i"):
            self.italic += 1
        elif tag == "blockquote":
            self._flush()
            self.block_prefix = "> "
        elif tag == "pre":
            self._flush()
            self.in_pre = True
            self.pre_buf = []
        elif tag == "table":
            self._flush()
            self._table_stack.append((self.rows, self.row))   # nested-table safe
            self.rows, self.row = [], []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self._cell_stack.append((self.cur, self.block_prefix))
            self.cur, self.block_prefix = [], ""
        elif tag in _BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag == "title" and not self.in_svg:
            self.in_title = False
            return
        if tag == "svg" and self.in_svg:
            self.in_svg -= 1
            if self.in_svg == 0:
                md, stats = _render_svg(self._svg_content, self._svg_labels)
                if stats["ok"]:
                    self._emit_block(md)        # mermaid or labels, inline
                else:
                    self.svgs += 1              # textless -> flag like an image
                self._svg_content, self._svg_labels = None, []
            return
        if self.in_svg:
            if tag == "text" and self._svg_in_text:
                t = " ".join("".join(self._svg_buf).split())
                if t:
                    self._svg_labels.append((self._svg_xy[0], self._svg_xy[1], t))
                self._svg_in_text = False
            return
        if self._drop(tag):
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if tag in ("strong", "b"):
            self.bold = max(0, self.bold - 1)
        elif tag in ("em", "i"):
            self.italic = max(0, self.italic - 1)
        elif tag == "a":
            if self._href:
                self.cur.append(f"]({self._href})")
            self._href = ""
        elif tag in ("ul", "ol"):
            self.list_depth = max(0, self.list_depth - 1)
        elif tag == "pre":
            code = "".join(self.pre_buf).strip("\n")
            self.in_pre = False
            self.pre_buf = []
            if code:
                self.out.append("```\n" + code + "\n```")
        elif tag in ("td", "th"):
            cell = _WS.sub(" ", "".join(self.cur).strip()).replace("|", "\\|")
            if self._cell_stack:
                self.cur, self.block_prefix = self._cell_stack.pop()
            else:
                self.cur = []
            self.row.append(cell)
        elif tag == "tr":
            if self.row:
                self.rows.append(self.row)
                self.row = []
        elif tag == "table":
            self._emit_table()
            self.rows, self.row = (self._table_stack.pop()
                                   if self._table_stack else ([], []))
        elif tag in _PREFIX_OWNERS:
            self._flush()
            self.block_prefix = ""      # an empty owner can't leak its prefix
        elif tag in _BLOCK:
            self._flush()

    def handle_data(self, data):
        if self.in_title:
            self.title += data
            return
        if self.in_svg:
            if self._svg_in_text:
                self._svg_buf.append(data)
            return
        if self.skip:
            return
        if self.in_pre:
            self.pre_buf.append(data)
            return
        t = _WS.sub(" ", data)
        if not t.strip():
            # whitespace-only node between elements — keep a single separator
            if self.cur and not self.cur[-1].endswith(" "):
                self.cur.append(" ")
            return
        lead = " " if t[:1] == " " else ""
        trail = " " if t[-1:] == " " else ""
        core = t.strip()
        if self.bold:
            core = f"**{core}**"
        if self.italic:
            core = f"_{core}_"
        self.cur.append(lead + core + trail)

    # -- table emit --
    def _emit_table(self):
        # Drop layout/spacer rows where every cell is blank (common in infoboxes)
        # so they don't leak as `|  |  |` noise; keep every row with content.
        rows = [r for r in self.rows if any(c.strip() for c in r)]
        if not rows:
            return                      # empty/degenerate (often a layout table)
        if self._cell_stack:
            # A table nested inside a cell — Markdown can't nest tables, so flatten
            # its content into the cell text rather than lose or corrupt it.
            text = " ".join(c for r in rows for c in r if c.strip())
            if self.cur and not self.cur[-1].endswith(" "):
                self.cur.append(" ")
            self.cur.append(text)
            return
        self._flush()
        width = max(len(r) for r in rows)
        padded = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(padded[0]) + " |",
                 "| " + " | ".join(["---"] * width) + " |"]
        for r in padded[1:]:
            lines.append("| " + " | ".join(r) + " |")
        self.out.append("\n".join(lines))
        self.tables += 1

    def _finalize(self):
        # Truncated input is common in agent contexts (a fetch cut off, an upstream
        # size cap). Finalize any block left open so its content isn't lost.
        if self.in_svg:
            md, stats = _render_svg(self._svg_content, self._svg_labels)
            if stats["ok"]:
                self._emit_block(md)
            elif stats["labels"] == 0 and self._svg_content is None:
                self.svgs += 1
            self.in_svg = 0
            self._svg_content, self._svg_labels = None, []
        if self.in_pre and self.pre_buf:
            code = "".join(self.pre_buf).strip("\n")
            self.in_pre = False
            self.pre_buf = []
            if code:
                self.out.append("```\n" + code + "\n```")
        while True:
            if self.row:
                self.rows.append(self.row)
                self.row = []
            self._cell_stack = []       # close any half-open cell so emit reaches out
            if self.rows:
                self._emit_table()
                self.rows = []
            if self._table_stack:
                self.rows, self.row = self._table_stack.pop()
            else:
                break

    def result(self) -> Tuple[str, dict]:
        self._finalize()
        self._flush()
        body = "\n\n".join(self.out)
        head = f"# {self.title.strip()}\n\n" if self.title.strip() else ""
        md = (head + body).strip()
        parts = []
        if self.images:
            parts.append(f"{self.images} image(s)")
        if self.svgs:
            parts.append(f"{self.svgs} diagram(s)/SVG")
        if parts:
            md += (f"\n\n> _[justokenmax: {' and '.join(parts)} not extracted — "
                   "read the source if visual data matters]_")
        md = md + "\n"
        if len(md) > MAX_OUTPUT_CHARS:
            md = md[:MAX_OUTPUT_CHARS] + _TRUNCATED
        return md, {"ok": self.saw_tag, "blocks": len(self.out),
                    "images": self.images, "svgs": self.svgs,
                    "tables": self.tables}


def html_to_markdown(raw: str) -> Tuple[str, dict]:
    """Return (markdown, stats). stats = {ok, blocks, images, tables}.

    ok is False when the input has no markup at all (not HTML) — optimize() then
    fails open and leaves the file untouched.
    """
    p = _Extractor()
    p.feed(raw)
    p.close()
    return p.result()
