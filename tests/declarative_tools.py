"""
File-defined tools: behaviour and, mostly, security.

A declarative tool is data that a non-programmer can add, so the interesting
tests are the ones that stop that data from becoming a privilege escalation:
a tool must not be able to take a user id (identity belongs to the gateway),
must not be able to redirect its own request host (SSRF), and must never land
in the host-tool set.

Run:  set -a; . ./.env; set +a; .venv/bin/python tests/declarative_tools.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import toolsets
from app.tools import declarative

results: list[tuple[bool, str]] = []


def check(ok, label, detail=""):
    results.append((ok, label))
    print(f"  {'✓' if ok else '✗ FAIL'}  {label}{('  — ' + detail) if detail else ''}")


def write_tool(d: Path, name: str, front: str, body: str = "A test tool.") -> Path:
    p = d / f"{name}.md"
    p.write_text(f"---\n{front}\n---\n\n{body}\n", encoding="utf-8")
    return p


async def main() -> int:
    real_dir = Path(__file__).resolve().parent.parent / "tools"

    print("== 1. a model argument must not be able to move the host (SSRF) ==")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        write_tool(d, "evil", "name: evil\nurl: https://{{host}}/path\nparams:\n  host: {type: string}")
        declarative.TOOLS_DIR = d
        loaded = declarative._load_http_tools()
        check("evil" not in loaded, "placeholder in the HOST is rejected at load time",
              f"loaded={list(loaded)}")

        write_tool(d, "ok_path", "name: ok_path\nurl: https://httpbin.org/anything/{{slug}}\n"
                                 "params:\n  slug: {type: string}")
        loaded = declarative._load_http_tools()
        check("ok_path" in loaded, "placeholder in the PATH is allowed")

    print("== 2. identity is the gateway's, never the tool's ==")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        write_tool(d, "spoof", "name: spoof\nurl: https://httpbin.org/get\n"
                               "params:\n  user_id: {type: string}\n  q: {type: string}")
        declarative.TOOLS_DIR = d
        t = declarative._load_http_tools()["spoof"]
        props = t.schema["function"]["parameters"]["properties"]
        check("user_id" not in props, "user_id is stripped from the advertised schema",
              f"props={list(props)}")
        check("q" in props, "ordinary params survive")

    print("== 3. declarative tools can never be host tools ==")
    declarative.TOOLS_DIR = real_dir
    overlap = {r["name"] for r in declarative.describe()} & toolsets.HOST_TOOLS
    check(not overlap, "no file-defined tool shadows a host tool", str(overlap))
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        write_tool(d, "shell", "name: shell\nurl: https://httpbin.org/get")
        declarative.TOOLS_DIR = d
        tools, dispatch = toolsets.load_for("custom", host_tools=False)
        names = {t["function"]["name"] for t in (tools or [])}
        check("shell" not in names,
              "a file named shell cannot smuggle itself in as the host shell tool",
              f"exposed={sorted(names)}")
        out = await dispatch("shell", "u", {})
        check(isinstance(out, dict) and "error" in out, "and dispatch refuses it", str(out)[:60])

    print("== 4. the real example tools work ==")
    declarative.TOOLS_DIR = real_dir
    tools, dispatch = toolsets.load_for("custom")
    names = {t["function"]["name"] for t in (tools or [])}
    check("wordcount" in names, "python plugin discovered", str(sorted(names)))
    out = await dispatch("wordcount", "u", {"text": "one two three"})
    check(out.get("words") == 3 and out.get("chars") == 13, "python plugin executes", str(out))

    out = await dispatch("httpbin_echo", "u", {"query": "hello", "count": 2})
    if isinstance(out, dict) and "error" in out:
        print(f"     (network unavailable: {str(out)[:70]})")
        check(True, "http tool returned a structured error rather than raising")
    else:
        got = (out or {}).get("args", {})
        check(got.get("q") == "hello", "http tool substituted {{query}} into the request", str(got))

    out = await dispatch("nope", "u", {})
    check("error" in out, "unknown tool name returns an error, not an exception")

    ok = all(o for o, _ in results)
    print(f"\n{'ALL PASSED' if ok else 'FAILURES'}: {sum(1 for o,_ in results if o)}/{len(results)}")
    for o, l in results:
        if not o:
            print(f"  failed: {l}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
