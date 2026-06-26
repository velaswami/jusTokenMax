"""HTML -> Markdown extraction (optimize kind="html").

HTML is the one Office-ish format we can parse with the standard library
(`html.parser`), so this handler adds NO dependency — keeping the zero-dependency
promise. It is COMPRESSION, not conversion: scripts, styles, navigation chrome,
and attributes are dropped, so a real page shrinks ~80-95% while its skeleton
(heading hierarchy, lists, tables, code, links) is preserved as Markdown.
"""

import json

from justokenmax.html import html_to_markdown
from justokenmax.optimize import optimize


# ---- structure preservation ----

def test_title_becomes_h1():
    md, _ = html_to_markdown(
        "<html><head><title>Pricing Guide</title></head>"
        "<body><p>hello</p></body></html>")
    assert "# Pricing Guide" in md


def test_headings_preserve_hierarchy():
    md, _ = html_to_markdown(
        "<body><h1>Top</h1><h2>Mid</h2><h3>Low</h3></body>")
    assert "# Top" in md
    assert "## Mid" in md
    assert "### Low" in md


def test_paragraphs_are_separated():
    md, _ = html_to_markdown("<body><p>first para</p><p>second para</p></body>")
    assert "first para" in md and "second para" in md
    assert "first parasecond" not in md      # not glued together


def test_nested_lists_preserve_depth():
    md, _ = html_to_markdown(
        "<body><ul><li>Top</li><ul><li>Nested</li></ul></ul></body>")
    assert "- Top" in md
    assert "  - Nested" in md


def test_bold_and_italic_with_boundary_space():
    # The hard part: emphasis markers must hug the word, with surrounding spaces
    # OUTSIDE the markers, or Markdown won't render them.
    md, _ = html_to_markdown(
        "<body><p>a <strong>bold </strong>and <em>italic</em> x</p></body>")
    assert "**bold**" in md
    assert "**bold **" not in md
    assert "_italic_" in md


def test_inline_whitespace_between_tags_is_kept():
    # Text split across inline tags must not collapse into one word.
    md, _ = html_to_markdown(
        "<body><p>See <a href='/plans'>plans</a> now</p></body>")
    assert "See [plans](/plans) now" in md


def test_link_keeps_text_and_href():
    md, _ = html_to_markdown(
        "<body><p><a href='https://x.com/a'>click</a></p></body>")
    assert "[click](https://x.com/a)" in md


def test_code_block_is_fenced_and_preserves_whitespace():
    md, _ = html_to_markdown(
        "<body><pre><code>def f():\n    return 1</code></pre></body>")
    assert "```" in md
    assert "    return 1" in md              # leading indent preserved


# ---- nested blocks (structure must survive real CMS/rendered HTML) ----

def test_list_item_with_inner_paragraph_keeps_bullet():
    md, _ = html_to_markdown("<body><ul><li><p>item</p></li></ul></body>")
    assert "- item" in md


def test_blockquote_with_inner_paragraph_keeps_marker():
    md, _ = html_to_markdown(
        "<body><blockquote><p>quoted</p></blockquote></body>")
    assert "> quoted" in md


def test_empty_heading_does_not_leak_prefix():
    md, _ = html_to_markdown("<body><h1></h1><p>para</p></body>")
    assert "# para" not in md          # the empty H1's '#' must not bleed onto para
    assert "para" in md


def test_table_cell_with_inner_paragraph_stays_in_cell():
    md, _ = html_to_markdown(
        "<body><table><tr><td><p>cell</p></td><td>b</td></tr></table></body>")
    assert "| cell | b |" in md         # 'cell' inside the cell, not leaked out


def test_definition_list_does_not_glue():
    md, _ = html_to_markdown(
        "<body><dl><dt>Term</dt><dd>Def</dd></dl></body>")
    assert "TermDef" not in md          # not glued into one word


def test_emphasis_does_not_leak_across_blocks():
    md, _ = html_to_markdown(
        "<body><p>a <strong>bold</p><p>next para</p></body>")
    assert "**next para**" not in md    # unclosed <strong> must not bold next block


def test_link_without_href_is_plain_text():
    md, _ = html_to_markdown("<body><p><a>label</a> text</p></body>")
    assert "label text" in md
    assert "[label]()" not in md


# ---- truncated input (EOF finalization) ----

def test_unclosed_table_still_emits():
    md, stats = html_to_markdown(
        "<body><table><tr><td>row content</td></tr>")  # no </table>, no </body>
    assert "row content" in md


def test_unclosed_pre_still_emits():
    md, _ = html_to_markdown("<body><pre>code line here")  # no </pre>
    assert "code line here" in md


# ---- tables ----

def test_table_rendered_as_markdown():
    md, stats = html_to_markdown(
        "<body><table><tr><th>Plan</th><th>Price</th></tr>"
        "<tr><td>Team</td><td>$10</td></tr></table></body>")
    assert "| Plan | Price |" in md
    assert "| Team | $10 |" in md
    assert stats["tables"] == 1


def test_nested_table_does_not_corrupt_outer():
    # Markdown can't nest tables; the inner table flattens into the cell and the
    # outer cell's own text ("BEFORE"/"AFTER") must not be lost or split out.
    md, _ = html_to_markdown(
        "<body><table><tr><td>BEFORE"
        "<table><tr><td>IN</td></tr></table>AFTER</td></tr></table></body>")
    assert "BEFORE" in md and "IN" in md and "AFTER" in md
    # all three live inside a single outer cell row, not scattered as loose text
    cell_line = [ln for ln in md.splitlines() if "BEFORE" in ln][0]
    assert "IN" in cell_line and "AFTER" in cell_line


def test_pipe_in_cell_is_escaped():
    md, _ = html_to_markdown(
        "<body><table><tr><td>a|b</td><td>c</td></tr></table></body>")
    assert "a\\|b" in md


def test_empty_table_is_skipped():
    md, stats = html_to_markdown(
        "<body><table><tr><td></td><td></td></tr></table><p>real</p></body>")
    assert "| --- |" not in md               # no degenerate empty table emitted
    assert stats["tables"] == 0
    assert "real" in md


# ---- entities ----

def test_empty_rows_within_table_are_dropped():
    # Real pages (e.g. infoboxes) interleave layout/spacer rows with content.
    # Empty rows must not leak as `|  |  |` noise, but content rows survive.
    md, stats = html_to_markdown(
        "<body><table>"
        "<tr><td>Key</td><td>Value</td></tr>"
        "<tr><td></td><td></td></tr>"
        "<tr><td>ext</td><td>.md</td></tr>"
        "</table></body>")
    assert "| ext | .md |" in md
    assert "|  |  |" not in md
    assert stats["tables"] == 1


def test_entities_are_unescaped():
    md, _ = html_to_markdown(
        "<body><p>Tom &amp; Jerry don&#x27;t&nbsp;quit</p></body>")
    assert "Tom & Jerry" in md
    assert "don't" in md
    assert "&amp;" not in md and "&#x27;" not in md


# ---- dropping noise ----

def test_script_and_style_dropped():
    md, _ = html_to_markdown(
        "<body><script>var secret=1;track();</script>"
        "<style>.a{color:red}</style><p>content</p></body>")
    assert "content" in md
    assert "track()" not in md and "color:red" not in md


def test_boilerplate_chrome_dropped():
    md, _ = html_to_markdown(
        "<body><nav>Home Docs</nav><p>article body</p>"
        "<footer>copyright 2026</footer></body>")
    assert "article body" in md
    assert "Home Docs" not in md
    assert "copyright 2026" not in md


# ---- SVG (flag, like images) ----

def test_inline_svg_text_is_extracted():
    # An inline SVG with text labels is now extracted (not dropped+flagged).
    md, stats = html_to_markdown(
        "<body><p>real prose here</p>"
        '<svg><text x="0" y="0">node A</text>'
        '<text x="0" y="50">node B</text></svg></body>')
    assert "node A" in md and "node B" in md
    assert "real prose here" in md
    assert stats["svgs"] == 0               # extracted, so not flagged


def test_textless_inline_svg_is_flagged():
    # A path-only SVG (icon) has nothing to extract -> still flagged like an image.
    md, stats = html_to_markdown(
        "<body><p>prose</p><svg><path d='M0 0 L9 9'/></svg></body>")
    assert stats["svgs"] == 1
    assert "diagram(s)/SVG not extracted" in md


def test_unclosed_inline_svg_still_emits():
    # truncated page: <svg> never closes — its captured label must not be lost
    md, _ = html_to_markdown(
        "<body><p>before</p><svg><text x='1' y='1'>kept label</text>")
    assert "kept label" in md


def test_inline_drawio_svg_becomes_mermaid():
    import base64, urllib.parse, zlib
    model = ('<mxGraphModel><root>'
             '<mxCell id="2" value="Plan" vertex="1"/>'
             '<mxCell id="3" value="Ship" vertex="1"/>'
             '<mxCell id="4" edge="1" source="2" target="3"/>'
             '</root></mxGraphModel>')
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    payload = urllib.parse.quote(
        base64.b64encode(c.compress(model.encode()) + c.flush()).decode())
    mxfile = f'<mxfile><diagram>{payload}</diagram></mxfile>'
    esc = mxfile.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    md, _ = html_to_markdown(
        f'<body><svg content="{esc}"><text>fallback</text></svg></body>')
    assert "```mermaid" in md
    assert "Plan" in md and "Ship" in md and "-->" in md


# ---- low-yield guard (JS/SVG/canvas pages -> fail open, keep the original) ----

def test_low_yield_js_app_page_fails_open(tmp_path):
    # A JS-rendered app: a big <script> and an empty root, almost no static text.
    # Replacing it with a near-empty digest would lose the page, so optimize()
    # must fail open and leave the original for the agent to read raw.
    p = tmp_path / "app.html"
    p.write_text("<!doctype html><html><head><title>App</title></head><body>"
                 "<div id='root'></div>"
                 "<script>" + "renderApp();" * 800 + "</script>"
                 "</body></html>", encoding="utf-8")
    res = optimize(str(p))
    assert res.ok is False and res.kind == "skip"
    assert "little" in res.note.lower() or "js" in res.note.lower()


def test_content_page_is_not_skipped_by_guard(tmp_path):
    # A real content page must still be compressed (guard only catches near-empty).
    p = tmp_path / "page.html"
    p.write_text(_PAGE, encoding="utf-8")
    res = optimize(str(p))
    assert res.ok and res.kind == "html"


# ---- images ----

def test_images_flagged_and_counted():
    md, stats = html_to_markdown(
        "<body><p>text</p><img src='a.png'><img src='b.png'></body>")
    assert stats["images"] == 2
    assert "2 image(s) not extracted" in md


# ---- optimize() dispatch (compression contract) ----

_PAGE = ("<html><head><title>Doc</title>"
         "<style>" + ".x{}" * 400 + "</style>"
         "<script>" + "track();" * 400 + "</script></head>"
         "<body><nav>menu menu menu</nav>"
         "<h1>Heading</h1>" + "<p>Real sentence of content here.</p>" * 40 +
         "</body></html>")


def test_optimize_detects_and_compresses_html(tmp_path):
    p = tmp_path / "page.html"
    p.write_text(_PAGE, encoding="utf-8")
    res = optimize(str(p))
    assert res.ok and res.kind == "html"
    assert res.output.endswith(".html.md")
    assert res.tokens_after < res.tokens_before     # real compression


def test_sniff_detects_html_in_txt(tmp_path):
    # HTML that arrives without a .html name must still be caught by content.
    p = tmp_path / "saved.txt"
    p.write_text("<!doctype html>\n<html><body>" +
                 "<p>sentence of real content.</p>" * 200 +
                 "</body></html>", encoding="utf-8")
    res = optimize(str(p))
    assert res.ok and res.kind == "html"


def test_secret_in_html_is_masked(tmp_path):
    secret = "AK" + "IA" + "S" * 16
    p = tmp_path / "leak.html"
    p.write_text("<html><body><p>key " + secret + " keep safe</p>" +
                 "<p>padding sentence.</p>" * 300 + "</body></html>",
                 encoding="utf-8")
    res = optimize(str(p))
    assert res.ok
    artifact = open(res.output, encoding="utf-8").read()
    assert secret not in artifact


def test_small_html_is_skipped(tmp_path):
    p = tmp_path / "tiny.html"
    p.write_text("<html><body><p>hi</p></body></html>", encoding="utf-8")
    res = optimize(str(p))
    assert res.ok is False and res.kind == "skip"
    assert "small" in res.note


def test_plain_text_is_not_treated_as_html(tmp_path):
    # A .txt with no markup must not be parsed as HTML.
    p = tmp_path / "notes.txt"
    p.write_text("just some plain notes, no tags here at all.\n" * 200,
                 encoding="utf-8")
    res = optimize(str(p))
    assert res.kind != "html"


def test_cli_html_subcommand(tmp_path, capsys):
    from justokenmax.cli import main
    p = tmp_path / "page.html"
    p.write_text(_PAGE, encoding="utf-8")
    rc = main(["html", "--json", str(p)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True and out["kind"] == "html"


# ---- safety caps ----

def test_output_size_cap_truncates(monkeypatch):
    from justokenmax import html as html_mod
    monkeypatch.setattr(html_mod, "MAX_OUTPUT_CHARS", 50)
    md, _ = html_to_markdown("<body>" + "<p>a long sentence of content.</p>" * 50 +
                             "</body>")
    assert "truncated" in md
