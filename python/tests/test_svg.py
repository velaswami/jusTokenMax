"""SVG -> Markdown extraction (optimize kind="svg").

Two tiers, both deterministic and dependency-free:
  * Tier 1 (always): pull <text>/<tspan> labels in reading order. Robust — pure
    XML text, no geometry heuristics. Hits the token win (~97% on real diagrams).
  * Tier 2 (best-effort): if the SVG is a draw.io export, its source mxGraph is
    embedded (base64+deflate+url-encoded) in the root `content` attribute —
    decode it with stdlib and emit true Mermaid (nodes + edges). Any failure
    falls back to Tier 1, so fidelity-when-we-can, robust-always.
"""

import base64
import json
import urllib.parse
import zlib

from justokenmax.svg import svg_to_markdown
from justokenmax.optimize import optimize


def _drawio_svg(mxmodel: str, compress=True) -> str:
    """Build an SVG whose root `content` attr embeds a draw.io mxfile."""
    if compress:
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        payload = urllib.parse.quote(
            base64.b64encode(c.compress(mxmodel.encode()) + c.flush()).decode())
    else:
        payload = mxmodel
    mxfile = f'<mxfile><diagram>{payload}</diagram></mxfile>'
    esc = mxfile.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" content="{esc}">'
            f'<rect/><text x="10" y="10">fallback label</text></svg>')


_MODEL = ('<mxGraphModel><root>'
          '<mxCell id="2" value="Start" vertex="1"/>'
          '<mxCell id="3" value="Deploy" vertex="1"/>'
          '<mxCell id="4" edge="1" source="2" target="3"/>'
          '</root></mxGraphModel>')


# ---- Tier 1: label extraction ----

def test_extracts_text_labels():
    md, stats = svg_to_markdown(
        '<svg><text x="0" y="0">Build</text>'
        '<text x="0" y="50">Test</text></svg>')
    assert "Build" in md and "Test" in md
    assert stats["ok"] and stats["kind"] == "labels"


def test_labels_sorted_top_to_bottom():
    # lower element authored first must still render after the upper one
    md, _ = svg_to_markdown(
        '<svg><text x="0" y="90">LOWER</text>'
        '<text x="0" y="10">UPPER</text></svg>')
    assert md.index("UPPER") < md.index("LOWER")


def test_tspan_runs_are_concatenated():
    md, _ = svg_to_markdown(
        '<svg><text x="0" y="0"><tspan>Hello</tspan><tspan> World</tspan></text></svg>')
    assert "Hello World" in md


def test_textless_svg_is_not_ok():
    # only geometry, no text -> nothing to extract; caller flags it like an image
    md, stats = svg_to_markdown('<svg><rect/><path d="M0 0 L10 10"/></svg>')
    assert stats["ok"] is False


# ---- Tier 2: draw.io embedded source -> Mermaid ----

def test_drawio_compressed_becomes_mermaid():
    md, stats = svg_to_markdown(_drawio_svg(_MODEL, compress=True))
    assert "```mermaid" in md
    assert "Start" in md and "Deploy" in md
    assert "-->" in md                       # real edge recovered
    assert stats["kind"] == "mermaid"
    assert stats["edges"] == 1


def test_drawio_uncompressed_becomes_mermaid():
    md, stats = svg_to_markdown(_drawio_svg(_MODEL, compress=False))
    assert "```mermaid" in md and "-->" in md
    assert stats["kind"] == "mermaid"


def test_decompression_bomb_falls_back_safely():
    # A tiny compressed payload that expands past the cap must be rejected
    # (no OOM, fast) and fall back to label extraction — runs on untrusted input.
    huge = ("<mxGraphModel><root><mxCell id='2' value='x' vertex='1'/>"
            "<!--" + "A" * (16 * 1024 * 1024) + "--></root></mxGraphModel>")
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    payload = urllib.parse.quote(
        base64.b64encode(c.compress(huge.encode()) + c.flush()).decode())
    mxfile = f'<mxfile><diagram>{payload}</diagram></mxfile>'
    esc = mxfile.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    svg = f'<svg content="{esc}"><text x="0" y="0">safe label</text></svg>'
    md, stats = svg_to_markdown(svg)
    assert stats["kind"] != "mermaid"        # bomb rejected, not decoded
    assert "safe label" in md                # degraded to Tier 1


def test_mermaid_label_backticks_neutralized():
    model = ('<mxGraphModel><root>'
             '<mxCell id="2" value="```evil" vertex="1"/>'
             '<mxCell id="3" value="ok" vertex="1"/>'
             '<mxCell id="4" edge="1" source="2" target="3"/>'
             '</root></mxGraphModel>')
    md, _ = svg_to_markdown(_drawio_svg(model, compress=True))
    assert md.count("```") == 2              # only the two fences, no injection


def test_duplicate_labels_deduped():
    md, stats = svg_to_markdown(
        '<svg><text x="5" y="5">Login</text>'
        '<text x="5" y="5">Login</text></svg>')   # drop-shadow doubling
    assert stats["labels"] == 1
    assert md.count("Login") == 1


def test_drawio_garbage_falls_back_to_labels():
    # a content attr that isn't decodable must not crash — fall back to <text>
    bad = ('<svg content="&lt;mxfile&gt;&lt;diagram&gt;@@notbase64@@'
           '&lt;/diagram&gt;&lt;/mxfile&gt;">'
           '<text x="0" y="0">fallback label</text></svg>')
    md, stats = svg_to_markdown(bad)
    assert "fallback label" in md
    assert stats["kind"] == "labels"         # degraded safely


# ---- optimize() dispatch ----

def test_optimize_compresses_svg_file(tmp_path):
    # a verbose standalone .svg with real labels -> compressed digest
    body = "".join(f'<text x="0" y="{i*12}">node label {i}</text>' for i in range(40))
    paths = "".join(f'<path d="M{i}.123456 {i}.987654 L{i+1}.5 {i+2}.5"/>'
                    for i in range(200))
    p = tmp_path / "diagram.svg"
    p.write_text(f'<svg xmlns="http://www.w3.org/2000/svg">{paths}{body}</svg>',
                 encoding="utf-8")
    res = optimize(str(p))
    assert res.ok and res.kind == "svg"
    assert res.output.endswith(".svg.md")
    assert res.tokens_after < res.tokens_before


def test_textless_svg_file_fails_open(tmp_path):
    # a big path-only icon yields no text -> skip (leave raw), don't emit empty
    paths = "".join(f'<path d="M{i}.1 {i}.2 L{i+1}.3 {i+2}.4"/>' for i in range(400))
    p = tmp_path / "icon.svg"
    p.write_text(f'<svg xmlns="http://www.w3.org/2000/svg">{paths}</svg>',
                 encoding="utf-8")
    res = optimize(str(p))
    assert res.ok is False and res.kind == "skip"


def test_cli_svg_subcommand(tmp_path, capsys):
    from justokenmax.cli import main
    body = "".join(f'<text x="0" y="{i*12}">label {i}</text>' for i in range(60))
    pad = "".join(f'<path d="M{i}.1 {i}.2 L{i+1}.3 {i+2}.4"/>' for i in range(200))
    p = tmp_path / "d.svg"
    p.write_text(f'<svg xmlns="http://www.w3.org/2000/svg">{pad}{body}</svg>',
                 encoding="utf-8")
    rc = main(["svg", "--json", str(p)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is True and out["kind"] == "svg"
