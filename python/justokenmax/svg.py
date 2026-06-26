"""SVG -> Markdown extraction.

An SVG is a token sink: verbose XML, high-precision path coordinates, styling.
But the *meaning* an agent needs is the text — and, for tool-generated diagrams,
sometimes the embedded source. Two deterministic, dependency-free tiers:

  * Tier 2 (best-effort, preferred): a draw.io export embeds its source mxGraph
    in the root `content` attribute (base64 + raw-deflate + url-encoded). Decode
    it with the standard library and emit TRUE Mermaid (nodes + edges).
  * Tier 1 (always, robust): pull <text>/<tspan> labels in reading order
    (sorted by y, then x). Pure XML text — no geometry heuristics, never wrong.

Any failure in Tier 2 falls back to Tier 1, and a textless SVG returns ok=False
so the caller can flag it like an image. We deliberately do NOT reconstruct
edges/scales from raw geometry — that's the fragile, off-ethos guesswork.
"""

from __future__ import annotations

import base64
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from html.parser import HTMLParser
from typing import List, Optional, Tuple

MAX_OUTPUT_CHARS = 5_000_000
# Hard cap on the decoded draw.io mxGraph. The `content` attr is attacker-
# controlled and runs on a Read hook, so a tiny deflate payload that expands to
# gigabytes (a decompression bomb) must be rejected, not allocated.
MAX_DECOMP = 12 * 1024 * 1024

_TRUNCATED = "\n\n> _[justokenmax: output truncated — SVG exceeds safety caps]_\n"
_DIAGRAM = re.compile(r"<diagram[^>]*>(.*?)</diagram>", re.S)
_NONALNUM = re.compile(r"[^0-9A-Za-z]")


def _f(v) -> float:
    try:
        return float(re.findall(r"-?\d+\.?\d*", v or "")[0])
    except (IndexError, TypeError, ValueError):
        return 0.0


class _SvgParser(HTMLParser):
    """Collect the root `content` attr and (y, x, text) for each <text>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.content: Optional[str] = None
        self.labels: List[Tuple[float, float, str]] = []
        self._in_text = False
        self._buf: List[str] = []
        self._xy = (0.0, 0.0)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "svg" and self.content is None:
            self.content = a.get("content")
        elif tag == "text":
            self._in_text = True
            self._buf = []
            self._xy = (_f(a.get("y")), _f(a.get("x")))

    def handle_endtag(self, tag):
        if tag == "text" and self._in_text:
            t = " ".join("".join(self._buf).split())
            if t:
                self.labels.append((self._xy[0], self._xy[1], t))
            self._in_text = False

    def handle_data(self, data):
        if self._in_text:
            self._buf.append(data)


def _mid(cell_id: str) -> str:
    """A Mermaid-safe node id from an arbitrary mxCell id."""
    return "n" + _NONALNUM.sub("_", cell_id or "")


def drawio_to_mermaid(content: str):
    """Decode a draw.io `content` mxfile -> (mermaid, n_nodes, n_edges), or None
    on any failure (caller then falls back to label extraction)."""
    try:
        m = _DIAGRAM.search(content or "")
        if not m:
            return None
        payload = m.group(1).strip()
        if "<mxGraphModel" in payload:
            if len(payload) > MAX_DECOMP:        # giant literal model -> reject
                return None
            model = payload                      # uncompressed
        else:
            raw = base64.b64decode(urllib.parse.unquote(payload))
            d = zlib.decompressobj(-15)
            model = d.decompress(raw, MAX_DECOMP)
            if d.unconsumed_tail:                # expands past the cap -> bomb
                return None
            model = model.decode("utf-8")
        root = ET.fromstring(model)
        nodes, edges = {}, []
        for c in root.iter("mxCell"):
            if c.get("vertex") == "1" and c.get("value"):
                nodes[c.get("id")] = c.get("value")
            elif c.get("edge") == "1":
                edges.append((c.get("source"), c.get("target")))
        if not nodes:
            return None

        def lab(i):
            # Neutralize chars that could break out of the ```mermaid fence or
            # the node's quoted label (untrusted diagram content).
            v = (nodes.get(i) or i).replace("`", "'").replace('"', "'").replace("\n", " ")
            return f'{_mid(i)}["{v}"]'

        lines = ["graph TD"]
        linked = set()
        for s, t in edges:
            if s and t:
                lines.append(f"    {lab(s)} --> {lab(t)}")
                linked.update((s, t))
        for i in nodes:                          # isolated nodes (no edges)
            if i not in linked:
                lines.append(f"    {lab(i)}")
        return "\n".join(lines), len(nodes), len(edges)
    except Exception:
        return None


def _cap(md: str) -> str:
    return md if len(md) <= MAX_OUTPUT_CHARS else md[:MAX_OUTPUT_CHARS] + _TRUNCATED


def render(content: Optional[str], labels: List[Tuple[float, float, str]]) -> Tuple[str, dict]:
    """Shared renderer used by both the standalone .svg handler and the HTML
    handler's inline-<svg> path."""
    if content:                                  # Tier 2: draw.io -> Mermaid
        res = drawio_to_mermaid(content)
        if res:
            mermaid, n_nodes, n_edges = res
            md = "```mermaid\n" + mermaid + "\n```\n"
            if len(md) > MAX_OUTPUT_CHARS:        # keep the fence closed
                md = md[:MAX_OUTPUT_CHARS] + "\n```\n" + _TRUNCATED
            return md, {"ok": True, "kind": "mermaid", "labels": 0,
                        "nodes": n_nodes, "edges": n_edges}
    # Tier 1: labels in reading order, de-duplicated (exporters double-render
    # text for drop shadows — same text at the same coordinate).
    ordered, seen = [], set()
    for y, x, t in sorted(labels, key=lambda r: (r[0], r[1])):
        k = (round(y, 1), round(x, 1), t)
        if k not in seen:
            seen.add(k)
            ordered.append(t)
    if ordered:                                  # Tier 1: labels in reading order
        md = ("**Diagram (SVG) — text in approximate reading order:**\n\n"
              + "\n".join(f"- {t}" for t in ordered) + "\n")
        return _cap(md), {"ok": True, "kind": "labels", "labels": len(ordered),
                          "nodes": 0, "edges": 0}
    return "", {"ok": False, "kind": "none", "labels": 0, "nodes": 0, "edges": 0}


def svg_to_markdown(raw: str) -> Tuple[str, dict]:
    """Return (markdown, stats). stats = {ok, kind, labels, nodes, edges}.
    ok is False for a textless, source-less SVG (a pure icon/illustration) — the
    caller flags it like an image rather than emitting an empty digest."""
    p = _SvgParser()
    p.feed(raw)
    p.close()
    return render(p.content, p.labels)
