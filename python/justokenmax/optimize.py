"""Dispatch an attachment to the right optimizer, with caching + ledger."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Optional

from . import cache
from .config import is_enabled
from .redact import mask_secrets, redact
from .tokens import pdf_image_tokens, text_tokens

PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
LOG_EXTS = {".log"}
JSON_EXTS = {".json"}
# Newline-delimited JSON: a whole-file json.loads fails, so it needs its own
# line-by-line, shape-grouping path (see jsoncompress.compress_ndjson).
NDJSON_EXTS = {".ndjson", ".jsonl"}
NB_EXTS = {".ipynb"}
CSV_EXTS = {".csv", ".tsv"}
DIFF_EXTS = {".diff", ".patch"}
# HTML pages. Compression (drop scripts/styles/chrome, keep the content skeleton
# as Markdown) — stdlib parser, no dependency.
HTML_EXTS = {".html", ".htm"}
# SVG. Compression: extract text labels (and draw.io embedded source -> Mermaid),
# drop the verbose geometry. Stdlib only, no dependency.
SVG_EXTS = {".svg"}
# Source files we can compress to a signature-only skeleton (outline). Limited
# to extensions the symbol parser (codeindex.LANGS) actually understands, so we
# never promise an outline we can't produce.
CODE_EXTS = {
    ".py",
    ".js", ".jsx", ".mjs", ".cjs",
    ".ts", ".tsx",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".c", ".h", ".cc", ".cpp", ".hpp",
}
# Ambiguous extensions whose kind we decide by sniffing the content.
GENERIC_EXTS = {".txt", ".out", ".text", ""}

# Below this, a downscale/recompress isn't worth a new file.
IMAGE_MIN_BYTES = 200 * 1024
# Below this a log is already cheap; compressing adds churn for no gain.
LOG_MIN_BYTES = 8 * 1024
# Below this a JSON blob isn't worth compressing.
JSON_MIN_BYTES = 4 * 1024
# Above this a JSON blob is collapsed to an inferred schema rather than sampled.
JSON_SCHEMA_BYTES = 256 * 1024
# Below this an NDJSON stream isn't worth grouping.
NDJSON_MIN_BYTES = 4 * 1024
# Below this a CSV is small enough to read whole.
CSV_MIN_BYTES = 4 * 1024

# Below this a diff is small enough to read whole.
DIFF_MIN_BYTES = 4 * 1024
# Below this an HTML page is small enough to read whole.
HTML_MIN_BYTES = 4 * 1024
# If a non-trivial page yields fewer than this many tokens, its content isn't in
# the static HTML (JS/SVG/canvas-rendered). Replacing it with a near-empty digest
# would lose the page, so we fail open and leave the original.
HTML_MIN_YIELD_TOKENS = 64
# Below this an .svg is small enough to read whole (also skips tiny icons).
SVG_MIN_BYTES = 4 * 1024

# Below this a source file is short enough to read whole; an outline of a tiny
# file rarely beats just reading it.
CODE_MIN_BYTES = 6 * 1024


@dataclasses.dataclass
class OptimizeResult:
    ok: bool
    kind: str                 # "pdf"|"image"|"code"|"lockfile"|"minified"|"skip"|...
    source: str
    output: Optional[str]     # path to optimized artifact (None if skipped)
    tokens_before: int
    tokens_after: int
    cached: bool
    note: str = ""

    @property
    def tokens_saved(self) -> int:
        return max(0, self.tokens_before - self.tokens_after)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["tokens_saved"] = self.tokens_saved
        return d


def _redact(text: str) -> str:
    """Sanitize a text digest before it is written to the cache artifact.

    Secret masking (API keys, tokens, JWTs, password=/secret= pairs) runs
    UNCONDITIONALLY — a live credential must never be stored, even when the
    optional `redact` token-cutting pass (base64 / data-URI elision) is
    disabled. So every write path in `optimize()` is sanitized regardless of
    config. The imports are module-level (not lazy) so this is visible to
    static dataflow analysis.
    """
    if is_enabled("redact"):
        return redact(text)[0]          # full pass: blob elision + secret mask
    return mask_secrets(text)[0]        # safety only: always mask secrets


def _sniff(path: str) -> str:
    """Decide kind by content for ambiguous extensions (.txt/.out/no ext)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(65536)
    except OSError:
        return "skip"
    from .jsoncompress import looks_like_json
    if looks_like_json(head):
        return "json"
    # HTML delivered without a .html name (saved as .txt / no extension): a lot
    # of agent-context HTML arrives this way. Gate on a clear markup opener so a
    # plain text file that merely mentions <html> isn't misrouted.
    if head.lstrip()[:200].lower().lstrip("﻿").startswith(("<!doctype html", "<html")):
        return "html"
    # Minified/packed asset: a single physical line far longer than any source.
    from .lockfile import looks_minified
    if looks_minified(head, path):
        return "minified"
    # log-ish: many lines, or ANSI/timestamps present
    if "\x1b[" in head or head.count("\n") >= 50:
        return "log"
    return "skip"


def _kind_by_name(path: str) -> Optional[str]:
    """Decide kind by basename, BEFORE the extension switch.

    Lockfiles (package-lock.json, Cargo.lock, ...) share extensions with plain
    JSON/YAML but want their own dependency-table compressor; minified assets
    (.min.js/.min.css) are opaque generated blobs we stub out. Returns None when
    the name is unremarkable so `_kind_for` falls through to the ext switch.
    """
    from .lockfile import is_minified_name, lock_flavor
    if lock_flavor(path):
        return "lockfile"
    if is_minified_name(path):
        return "minified"
    return None


def _kind_for(path: str) -> str:
    named = _kind_by_name(path)
    if named:
        return named
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXTS:
        return "pdf"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in LOG_EXTS:
        return "log"
    if ext in JSON_EXTS:
        return "json"
    if ext in NDJSON_EXTS:
        return "ndjson"
    if ext in NB_EXTS:
        return "notebook"
    if ext in CSV_EXTS:
        return "csv"
    if ext in DIFF_EXTS:
        return "diff"
    if ext in HTML_EXTS:
        return "html"
    if ext in SVG_EXTS:
        return "svg"
    if ext in CODE_EXTS:
        return "code"
    if ext in GENERIC_EXTS:
        return _sniff(path)
    return "skip"


def optimize(
    path: str,
    quality: int = 80,
    max_edge: Optional[int] = None,
    record: bool = True,
) -> OptimizeResult:
    """Optimize a single attachment. Idempotent and cached."""
    if not os.path.isfile(path):
        return OptimizeResult(False, "skip", path, None, 0, 0, False,
                              note="not a file")

    kind = _kind_for(path)
    if kind == "skip":
        return OptimizeResult(False, "skip", path, None, 0, 0, False,
                              note="unsupported type")

    from .config import is_enabled
    if not is_enabled(kind):
        return OptimizeResult(False, "skip", path, None, 0, 0, False,
                              note="disabled by config")

    opts = {"kind": kind, "quality": quality, "max_edge": max_edge}

    if kind == "pdf":
        key, out = cache.cache_paths(path, opts, ".md")
        meta = cache.load_meta(key)
        if meta and out.exists():
            return OptimizeResult(True, "pdf", path, str(out),
                                  meta["tokens_before"], meta["tokens_after"],
                                  cached=True, note="cache hit")
        from .pdf import pdf_to_markdown
        md, n_pages = pdf_to_markdown(path)
        md = _redact(md)  # mask secrets before storing, like every other branch
        out.write_text(md, encoding="utf-8")
        # A PDF is billed as text + a per-page image. Markdown keeps the text
        # and drops the image channel, so the saving is exactly that channel.
        text_after = text_tokens(md)
        tokens_before = pdf_image_tokens(n_pages) + text_after
        tokens_after = text_after
        meta = {"tokens_before": tokens_before, "tokens_after": tokens_after,
                "pages": n_pages}
        cache.save_meta(key, meta)
        res = OptimizeResult(True, "pdf", path, str(out), tokens_before,
                             tokens_after, cached=False,
                             note=f"{n_pages} pages")

    elif kind == "log":
        if os.path.getsize(path) < LOG_MIN_BYTES:
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="log already small")
        key, out = cache.cache_paths(path, opts, ".log.txt")
        meta = cache.load_meta(key)
        if meta and out.exists():
            return OptimizeResult(True, "log", path, str(out),
                                  meta["tokens_before"], meta["tokens_after"],
                                  cached=True, note="cache hit")
        from .logs import compress_log
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        digest, stats = compress_log(raw)
        digest = _redact(digest)
        out.write_text(digest, encoding="utf-8")
        tokens_before = text_tokens(raw)
        tokens_after = text_tokens(digest)
        meta = {"tokens_before": tokens_before, "tokens_after": tokens_after,
                "lines": [stats["lines_before"], stats["lines_after"]]}
        cache.save_meta(key, meta)
        res = OptimizeResult(True, "log", path, str(out), tokens_before,
                             tokens_after, cached=False,
                             note=f"{stats['lines_before']} -> "
                                  f"{stats['lines_after']} lines")

    elif kind == "json":
        if os.path.getsize(path) < JSON_MIN_BYTES:
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="json already small")
        key, out = cache.cache_paths(path, opts, ".min.json")
        meta = cache.load_meta(key)
        if meta and out.exists():
            return OptimizeResult(True, "json", path, str(out),
                                  meta["tokens_before"], meta["tokens_after"],
                                  cached=True, note="cache hit")
        from .jsoncompress import compress_json, is_uniform_object_array
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        # Schema mode for the bulk case: a large blob, or a top-level uniform
        # array of objects (a table dump / RAG payload), collapses to one
        # inferred schema node instead of a head+tail sample.
        use_schema = os.path.getsize(path) > JSON_SCHEMA_BYTES
        if not use_schema:
            try:
                import json as _json
                use_schema = is_uniform_object_array(_json.loads(raw))
            except (ValueError, TypeError):
                use_schema = False
        digest, stats = compress_json(raw, schema=use_schema)
        if not stats.get("ok"):
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="not valid JSON")
        digest = _redact(digest)
        out.write_text(digest, encoding="utf-8")
        tokens_before = text_tokens(raw)
        tokens_after = text_tokens(digest)
        meta = {"tokens_before": tokens_before, "tokens_after": tokens_after}
        cache.save_meta(key, meta)
        res = OptimizeResult(True, "json", path, str(out), tokens_before,
                             tokens_after, cached=False,
                             note=f"{stats.get('mode', 'sample')}: "
                                  f"{stats['bytes_before']//1024}KB -> "
                                  f"{stats['bytes_after']//1024}KB")

    elif kind == "ndjson":
        if os.path.getsize(path) < NDJSON_MIN_BYTES:
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="ndjson already small")
        key, out = cache.cache_paths(path, opts, ".ndjson.txt")
        meta = cache.load_meta(key)
        if meta and out.exists():
            return OptimizeResult(True, "ndjson", path, str(out),
                                  meta["tokens_before"], meta["tokens_after"],
                                  cached=True, note="cache hit")
        from .jsoncompress import compress_ndjson
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        digest, stats = compress_ndjson(raw)
        if not stats.get("ok"):
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="not NDJSON")
        digest = _redact(digest)
        out.write_text(digest, encoding="utf-8")
        tokens_before = text_tokens(raw)
        tokens_after = text_tokens(digest)
        meta = {"tokens_before": tokens_before, "tokens_after": tokens_after}
        cache.save_meta(key, meta)
        res = OptimizeResult(True, "ndjson", path, str(out), tokens_before,
                             tokens_after, cached=False,
                             note=f"{stats['records']} records, "
                                  f"{stats['shapes']} shapes")

    elif kind == "lockfile":
        key, out = cache.cache_paths(path, opts, ".lock.txt")
        meta = cache.load_meta(key)
        if meta and out.exists():
            return OptimizeResult(True, "lockfile", path, str(out),
                                  meta["tokens_before"], meta["tokens_after"],
                                  cached=True, note="cache hit")
        from .lockfile import compress_lockfile, lock_flavor
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        digest, stats = compress_lockfile(raw, lock_flavor(path) or "")
        if not stats.get("ok"):
            # Fail-open: an unparseable lockfile passes through untouched.
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note=stats.get("note", "lockfile parse failed"))
        digest = _redact(digest)
        out.write_text(digest, encoding="utf-8")
        tokens_before = text_tokens(raw)
        tokens_after = text_tokens(digest)
        cache.save_meta(key, {"tokens_before": tokens_before,
                              "tokens_after": tokens_after})
        res = OptimizeResult(True, "lockfile", path, str(out), tokens_before,
                             tokens_after, cached=False,
                             note=f"{stats['flavor']}: {stats['packages']} packages")

    elif kind == "minified":
        key, out = cache.cache_paths(path, opts, ".min.txt")
        meta = cache.load_meta(key)
        if meta and out.exists():
            return OptimizeResult(True, "minified", path, str(out),
                                  meta["tokens_before"], meta["tokens_after"],
                                  cached=True, note="cache hit")
        from .lockfile import minified_stub
        # Don't read/tokenize the whole asset just to size it — a minified
        # bundle can be many MB, and reading it on the Read hot path defeats the
        # point of stubbing it. Estimate the original cost from the byte count
        # (~4 bytes/token), the same proxy used for other large-blob skips.
        n_bytes = os.path.getsize(path)
        digest, stats = minified_stub(n_bytes)
        out.write_text(digest, encoding="utf-8")
        tokens_before = n_bytes // 4
        tokens_after = text_tokens(digest)
        cache.save_meta(key, {"tokens_before": tokens_before,
                              "tokens_after": tokens_after})
        res = OptimizeResult(True, "minified", path, str(out), tokens_before,
                             tokens_after, cached=False,
                             note=f"{n_bytes // 1024}KB stubbed")

    elif kind == "notebook":
        key, out = cache.cache_paths(path, opts, ".ipynb.md")
        meta = cache.load_meta(key)
        if meta and out.exists():
            return OptimizeResult(True, "notebook", path, str(out),
                                  meta["tokens_before"], meta["tokens_after"],
                                  cached=True, note="cache hit")
        from .notebook import notebook_to_markdown
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        digest, stats = notebook_to_markdown(raw)
        if not stats.get("ok"):
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="not a notebook")
        digest = _redact(digest)
        out.write_text(digest, encoding="utf-8")
        tokens_before = text_tokens(raw)
        tokens_after = text_tokens(digest)
        cache.save_meta(key, {"tokens_before": tokens_before,
                              "tokens_after": tokens_after})
        res = OptimizeResult(True, "notebook", path, str(out), tokens_before,
                             tokens_after, cached=False,
                             note=f"{stats['cells']} cells, "
                                  f"{stats['images_elided']} images elided")

    elif kind == "csv":
        if os.path.getsize(path) < CSV_MIN_BYTES:
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="csv already small")
        key, out = cache.cache_paths(path, opts, ".csv.md")
        meta = cache.load_meta(key)
        if meta and out.exists():
            return OptimizeResult(True, "csv", path, str(out),
                                  meta["tokens_before"], meta["tokens_after"],
                                  cached=True, note="cache hit")
        from .csvtable import compress_csv
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        digest, stats = compress_csv(raw)
        if not stats.get("ok"):
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="empty csv")
        digest = _redact(digest)
        out.write_text(digest, encoding="utf-8")
        tokens_before = text_tokens(raw)
        tokens_after = text_tokens(digest)
        cache.save_meta(key, {"tokens_before": tokens_before,
                              "tokens_after": tokens_after})
        res = OptimizeResult(True, "csv", path, str(out), tokens_before,
                             tokens_after, cached=False,
                             note=f"{stats['rows']} rows x {stats['cols']} cols")

    elif kind == "diff":
        if os.path.getsize(path) < DIFF_MIN_BYTES:
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="diff already small")
        key, out = cache.cache_paths(path, opts, ".diff")
        meta = cache.load_meta(key)
        if meta and out.exists():
            return OptimizeResult(True, "diff", path, str(out),
                                  meta["tokens_before"], meta["tokens_after"],
                                  cached=True, note="cache hit")
        from .diffcompress import compress_diff
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        digest, stats = compress_diff(raw)
        digest = _redact(digest)
        out.write_text(digest, encoding="utf-8")
        tokens_before = text_tokens(raw)
        tokens_after = text_tokens(digest)
        cache.save_meta(key, {"tokens_before": tokens_before,
                              "tokens_after": tokens_after})
        res = OptimizeResult(True, "diff", path, str(out), tokens_before,
                             tokens_after, cached=False,
                             note=f"{stats['files_elided']}/{stats['files_total']} "
                                  f"files elided")

    elif kind == "html":
        if os.path.getsize(path) < HTML_MIN_BYTES:
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="html already small")
        key, out = cache.cache_paths(path, opts, ".html.md")
        meta = cache.load_meta(key)
        if meta and out.exists():
            return OptimizeResult(True, "html", path, str(out),
                                  meta["tokens_before"], meta["tokens_after"],
                                  cached=True, note="cache hit")
        from .html import html_to_markdown
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        try:
            digest, stats = html_to_markdown(raw)
        except Exception:
            # Fail-open: the Read hook must never crash on pathological input.
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="html parse failed")
        if not stats.get("ok"):
            # Fail-open: no markup found -> not actually HTML.
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="not HTML")
        digest = _redact(digest)            # also elides inline data-URIs/base64
        tokens_before = text_tokens(raw)
        tokens_after = text_tokens(digest)
        if tokens_after < HTML_MIN_YIELD_TOKENS:
            # Near-empty extraction: the content is JS/SVG/canvas-rendered, not in
            # the static HTML. Leave the original so the agent can read it raw.
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="html yields too little text "
                                       "(likely JS/SVG-rendered) — left as-is")
        out.write_text(digest, encoding="utf-8")
        cache.save_meta(key, {"tokens_before": tokens_before,
                              "tokens_after": tokens_after,
                              "blocks": stats["blocks"],
                              "images": stats["images"],
                              "tables": stats["tables"]})
        note = f"{stats['blocks']} blocks, {stats['tables']} tables"
        if stats["images"]:
            note += f", {stats['images']} images flagged"
        res = OptimizeResult(True, "html", path, str(out), tokens_before,
                             tokens_after, cached=False, note=note)

    elif kind == "svg":
        if os.path.getsize(path) < SVG_MIN_BYTES:
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="svg already small")
        key, out = cache.cache_paths(path, opts, ".svg.md")
        meta = cache.load_meta(key)
        if meta and out.exists():
            return OptimizeResult(True, "svg", path, str(out),
                                  meta["tokens_before"], meta["tokens_after"],
                                  cached=True, note="cache hit")
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        try:
            from .svg import svg_to_markdown
            digest, stats = svg_to_markdown(raw)
        except Exception:
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="svg parse failed")
        if not stats.get("ok"):
            # Textless icon/illustration — nothing to extract; leave it raw.
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="svg has no extractable text")
        digest = _redact(digest)
        out.write_text(digest, encoding="utf-8")
        tokens_before = text_tokens(raw)
        tokens_after = text_tokens(digest)
        cache.save_meta(key, {"tokens_before": tokens_before,
                              "tokens_after": tokens_after,
                              "kind": stats["kind"], "labels": stats["labels"],
                              "nodes": stats["nodes"], "edges": stats["edges"]})
        note = (f"{stats['nodes']} nodes, {stats['edges']} edges (mermaid)"
                if stats["kind"] == "mermaid"
                else f"{stats['labels']} labels")
        res = OptimizeResult(True, "svg", path, str(out), tokens_before,
                             tokens_after, cached=False, note=note)

    elif kind == "code":
        if os.path.getsize(path) < CODE_MIN_BYTES:
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="source already small")
        key, out = cache.cache_paths(path, opts, ".outline.md")
        meta = cache.load_meta(key)
        if meta and out.exists():
            return OptimizeResult(True, "code", path, str(out),
                                  meta["tokens_before"], meta["tokens_after"],
                                  cached=True, note="cache hit")
        from .outline import file_outline
        try:
            digest, stats = file_outline(path)
        except Exception:  # fail-open: any parser error -> passthrough
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="outline failed")
        digest = _redact(digest)
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        tokens_before = text_tokens(raw)
        tokens_after = text_tokens(digest)
        # Only worth it if the skeleton is meaningfully smaller than the source.
        if not stats.get("ok") or tokens_after >= tokens_before * 3 // 4:
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="outline not smaller")
        out.write_text(digest, encoding="utf-8")
        cache.save_meta(key, {"tokens_before": tokens_before,
                              "tokens_after": tokens_after})
        res = OptimizeResult(True, "code", path, str(out), tokens_before,
                             tokens_after, cached=False,
                             note=f"{stats['symbols']} symbols ({stats['lang']})")

    else:  # image
        if os.path.getsize(path) < IMAGE_MIN_BYTES:
            return OptimizeResult(False, "skip", path, None, 0, 0, False,
                                  note="image already small")
        from .image import compress_image
        from .tokens import MAX_EDGE
        edge = max_edge or MAX_EDGE
        opts["max_edge"] = edge
        key, out = cache.cache_paths(path, opts, ".img")
        existing = cache.load_meta(key)
        if existing and existing.get("output") and os.path.exists(existing["output"]):
            return OptimizeResult(True, "image", path, existing["output"],
                                  existing["tokens_before"],
                                  existing["tokens_after"],
                                  cached=True, note="cache hit")
        out_path, stats = compress_image(path, str(out), max_edge=edge,
                                         quality=quality)
        stats["output"] = out_path
        cache.save_meta(key, stats)
        res = OptimizeResult(True, "image", path, out_path,
                             stats["tokens_before"], stats["tokens_after"],
                             cached=False,
                             note=f"{stats['bytes_before']//1024}KB -> "
                                  f"{stats['bytes_after']//1024}KB")

    if res.ok and res.output:
        # Reversibility: remember which original produced this artifact so
        # `justokenmax retrieve <artifact>` can hand the full version back.
        cache.record_origin(res.output, res.source)
    if record and res.ok and not res.cached:
        cache.record_savings(res.tokens_saved, res.kind)
    return res
