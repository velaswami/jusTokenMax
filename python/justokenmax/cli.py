"""jusTokenMax command line (`justokenmax`, also installed as `justokenmax`).

    justokenmax optimize FILE...    # auto-dispatch attachments/logs by type
    justokenmax pdf FILE            # force PDF -> markdown
    justokenmax image FILE          # force image / screenshot compression
    justokenmax logs FILE           # compress a verbose log
    justokenmax index [PATH]        # build the code symbol index
    justokenmax query TERM          # look up a symbol -> file:line + signature
    justokenmax stats               # lifetime savings ledger

Add --json for machine-readable output (used by the Claude Code hooks).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, cache
from .optimize import optimize


def _print(result_dict: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result_dict))
        return
    r = result_dict
    if not r["ok"]:
        print(f"skip  {r['source']}  ({r['note']})")
        return
    saved = r["tokens_saved"]
    pct = (100 * saved // r["tokens_before"]) if r["tokens_before"] else 0
    tag = "cache" if r["cached"] else r["kind"]
    print(f"ok    {r['source']}")
    print(f"      -> {r['output']}")
    print(f"      {r['tokens_before']} -> {r['tokens_after']} tokens "
          f"(-{saved}, -{pct}%)  [{tag}]  {r['note']}")


def _run_optimize(args) -> int:
    results = [optimize(f, quality=args.quality, max_edge=args.max_edge).to_dict()
               for f in args.files]
    if getattr(args, "json", False):
        print(json.dumps(results if len(results) > 1 else results[0]))
    else:
        for r in results:
            _print(r, as_json=False)
    return 0 if any(r["ok"] for r in results) else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="justokenmax",
                                description="jusTokenMax — token-reduction toolkit.")
    p.add_argument("--version", action="version", version=f"justokenmax {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--quality", type=int, default=80, help="image JPEG quality")
    common.add_argument("--max-edge", type=int, default=None,
                        help="override max image long edge (px)")

    for name in ("optimize", "pdf", "image", "logs", "json", "html", "svg"):
        sub.add_parser(name, parents=[common]).add_argument("files", nargs="+")

    pr = sub.add_parser("retrieve", help="get the original behind an optimized artifact")
    pr.add_argument("artifact")
    pr.add_argument("--json", action="store_true")

    pd = sub.add_parser("delta", help="return only what changed since last read")
    pd.add_argument("files", nargs="+")
    pd.add_argument("--json", action="store_true")

    prd = sub.add_parser("redact", help="strip base64 blobs and mask secrets")
    prd.add_argument("files", nargs="+")
    prd.add_argument("--json", action="store_true")

    po = sub.add_parser("outline", help="a file's signatures + line numbers, no bodies")
    po.add_argument("files", nargs="+")
    po.add_argument("--json", action="store_true")

    pdf_ = sub.add_parser("diff", help="compress a git diff (elide lockfile/generated noise)")
    pdf_.add_argument("path", nargs="?", default="-",
                      help="diff file, or '-'/omitted to read stdin")
    pdf_.add_argument("--json", action="store_true")

    pi = sub.add_parser("index", help="build the code symbol index")
    pi.add_argument("path", nargs="?", default=".")
    pi.add_argument("--json", action="store_true")

    pq = sub.add_parser("query", help="look up a symbol in the index")
    pq.add_argument("term")
    pq.add_argument("--root", default=".")
    pq.add_argument("--kind", default=None)
    pq.add_argument("--limit", type=int, default=50)
    pq.add_argument("--json", action="store_true")

    pdisc = sub.add_parser("discover",
                           help="survey Claude Code history for recoverable tokens "
                                "+ the unsupported-extension backlog")
    pdisc.add_argument("--root", default=None,
                       help="history dir (default: ~/.claude/projects)")
    pdisc.add_argument("--json", action="store_true")

    sub.add_parser("stats").add_argument("--json", action="store_true")
    sub.add_parser("sessions", help="per-session savings (effectiveness over time)").add_argument("--json", action="store_true")
    sub.add_parser("session-end", help="record the current session's savings (used by the Stop hook)").add_argument("--session-id", default=None)
    sub.add_parser("mcp", help="run the MCP server over stdio (any MCP agent)")

    pxy = sub.add_parser("proxy",
                         help="compress a downstream MCP server's output for any agent")
    pxy.add_argument("downstream", nargs=argparse.REMAINDER,
                     help="-- <command ...> for the downstream MCP server")

    pc = sub.add_parser("config", help="show or toggle which levers are enabled")
    pc.add_argument("action", nargs="?", choices=("enable", "disable"),
                    help="enable/disable a lever (omit to show current config)")
    pc.add_argument("kind", nargs="?",
                    help="pdf|image|log|json|ndjson|notebook|csv|diff|code|html|lockfile|minified|redact")
    pc.add_argument("--json", action="store_true")

    from .install import AGENTS
    for verb, helptext in (("install", "register the MCP server with a coding agent"),
                           ("uninstall", "remove the MCP server from a coding agent")):
        sp = sub.add_parser(verb, help=helptext)
        sp.add_argument("agent", nargs="?", choices=AGENTS,
                        help=f"{'|'.join(AGENTS)} (default: auto-detect)")
        sp.add_argument("--dry-run", action="store_true",
                        help="show what would change without writing")
        sp.add_argument("--json", action="store_true")

    args = p.parse_args(argv)

    if args.cmd == "discover":
        from .discover import discover, format_report
        report = discover(root=args.root)
        if args.json:
            print(json.dumps(report))
        else:
            print(format_report(report))
        return 0

    if args.cmd == "stats":
        led = cache.read_ledger()
        if args.json:
            print(json.dumps(led))
        else:
            total = led.get("total_tokens_saved", 0)
            print(f"justokenmax: {total:,} tokens saved across {led.get('runs', 0)} runs")
            for kind, n in sorted(led.get("by_kind", {}).items()):
                print(f"  {kind:6} {n:,}")
        return 0

    if args.cmd == "index":
        from .codeindex import build_index
        idx = build_index(args.path)
        if args.json:
            print(json.dumps({"files": idx["files"],
                              "symbols": len(idx["symbols"]),
                              "root": idx["root"]}))
        else:
            print(f"indexed {len(idx['symbols'])} symbols across "
                  f"{idx['files']} files -> "
                  f"{os.path.join(idx['root'], '.justokenmax', 'index.json')}")
        return 0

    if args.cmd == "query":
        from .codeindex import query, format_hits
        hits = query(args.root, args.term, kind=args.kind, limit=args.limit)
        if args.json:
            print(json.dumps(hits))
        else:
            print(format_hits(hits))
        return 0 if hits else 1

    if args.cmd == "delta":
        from .delta import delta
        rc = 1
        for f in args.files:
            artifact, st = delta(f)
            if args.json:
                print(json.dumps({"file": f, **st}))
            else:
                saved = max(0, st["tokens_full"] - st["tokens_delta"])
                pct = (100 * saved // st["tokens_full"]) if st["tokens_full"] else 0
                tag = "first read (full)" if not st["had_prior"] else \
                      ("no change" if not st["changed"] else f"diff -{pct}%")
                print(f"# {f}  [{tag}]  "
                      f"{st['tokens_full']}->{st['tokens_delta']} tokens")
                print(artifact)
            if st["had_prior"]:
                rc = 0
        return rc

    if args.cmd == "redact":
        from .redact import redact
        results = []
        for f in args.files:
            raw = Path(f).read_text(encoding="utf-8", errors="replace")
            clean, st = redact(raw)
            key, out = cache.cache_paths(f, {"kind": "redact"}, ".redacted.txt")
            out.write_text(clean, encoding="utf-8")
            cache.record_origin(str(out), f)
            st.update({"file": f, "output": str(out)})
            results.append(st)
            if not args.json:
                print(f"ok    {f} -> {out}")
                print(f"      {st['secrets_masked']} secrets masked, "
                      f"{st['blobs_elided']} blobs elided")
        if args.json:
            print(json.dumps(results if len(results) > 1 else results[0]))
        return 0

    if args.cmd == "outline":
        from .outline import file_outline
        rc = 1
        results = []
        for f in args.files:
            text, st = file_outline(f)
            if args.json:
                results.append({"file": f, **st, "outline": text})
            else:
                if st["ok"]:
                    print(text, end="")
                else:
                    print(f"skip  {f}  ({st['note']})")
            if st["ok"]:
                rc = 0
        if args.json:
            print(json.dumps(results if len(results) > 1 else results[0]))
        return rc

    if args.cmd == "sessions":
        from . import sessions as sess
        s = sess.summary()
        if args.json:
            print(json.dumps(s))
        else:
            print(f"justokenmax: {s['tokens_saved']:,} tokens saved across "
                  f"{s['sessions']} sessions (avg {s['avg_per_session']:,}/session)")
            for kind, n in sorted(s["by_kind"].items()):
                print(f"  {kind:9} {n:,}")
            if s["recent"]:
                print("  recent:")
                for r in s["recent"]:
                    print(f"    {r.get('ended','?')}  -{r['tokens_saved']:,}  "
                          f"({r['runs']} runs)")
        return 0

    if args.cmd == "session-end":
        from . import sessions as sess
        row = sess.record(session_id=args.session_id)
        if row:
            print(json.dumps(row))
        return 0

    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return 0

    if args.cmd == "proxy":
        from .proxy import run
        down = args.downstream
        if down and down[0] == "--":
            down = down[1:]
        return run(down)

    if args.cmd == "config":
        from . import config as cfg
        if args.action:
            if not args.kind:
                print(f"usage: justokenmax config {args.action} <kind>")
                return 1
            try:
                cfg.set_kind(args.kind, enabled=(args.action == "enable"))
            except ValueError as e:
                print(str(e))
                return 1
        s = cfg.summary()
        if args.json:
            print(json.dumps(s))
        else:
            print(f"config: {s['config_file']}")
            for k, on in s["kinds"].items():
                print(f"  {k:9} {'on' if on else 'OFF'}")
        return 0

    if args.cmd in ("install", "uninstall"):
        from . import install as inst
        agents = [args.agent] if args.agent else inst.detect()
        fn = inst.install if args.cmd == "install" else inst.uninstall
        results = [fn(a, dry_run=args.dry_run) for a in agents]
        if args.json:
            print(json.dumps(results))
        else:
            tag = " (dry-run)" if args.dry_run else ""
            for r in results:
                print(f"{r['agent']:9} {r['status']}{tag}  ->  {r['path']}")
            if not results:
                print("no agents detected — specify one: "
                      f"{', '.join(inst.AGENTS)}")
        return 0

    if args.cmd == "diff":
        from .diffcompress import compress_diff
        raw = (sys.stdin.read() if args.path == "-"
               else Path(args.path).read_text(encoding="utf-8", errors="replace"))
        digest, st = compress_diff(raw)
        if args.json:
            print(json.dumps({**st, "diff": digest}))
        else:
            sys.stdout.write(digest)
        return 0

    if args.cmd == "retrieve":
        origin = cache.lookup_origin(args.artifact)
        if args.json:
            print(json.dumps({"artifact": args.artifact, "origin": origin}))
        elif origin:
            print(origin)
        else:
            print("no recorded original for that artifact")
        return 0 if origin else 1

    return _run_optimize(args)


if __name__ == "__main__":
    sys.exit(main())
