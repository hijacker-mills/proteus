"""
`proteus tui` — a full-screen console for trying agents out.

The CLI's `chat` is fine for one conversation. This is for the thing you
actually do while building an agent: pick one, talk to it, watch which tools it
reaches for and how long each takes, change your mind, try another, all without
losing the transcript or retyping curl.

It talks to a RUNNING gateway over HTTP, the same as any other client. It does
not import the agent loop, so what you see here is what a real caller gets,
including the streaming behaviour and the tool events.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx
from ..identity import signed_headers
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (Footer, Header, Input, ListItem, ListView, Log,
                             Static)


class ProteusTUI(App):
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #sidebar { width: 32; border-right: solid $primary-darken-2; }
    #agents { height: 1fr; }
    #detail { height: auto; padding: 0 1; color: $text-muted; }
    #main { width: 1fr; }
    #transcript { height: 1fr; border: none; padding: 0 1; }
    #status { height: auto; padding: 0 1; color: $text-muted; }
    Input { dock: bottom; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "quit"),
        Binding("ctrl+r", "reset", "reset chat"),
        Binding("ctrl+l", "clear", "clear log"),
        Binding("f5", "refresh", "reload agents"),
    ]

    def __init__(self, base: str, key: str, user: str) -> None:
        super().__init__()
        self.base = base.rstrip("/")
        self.key = key
        self.user = user
        self.agents: list[dict[str, Any]] = []
        self.current: str | None = None
        self.history: list[dict[str, str]] = []
        self.busy = False
        self.last_status = ""

    # ── layout ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("[b]agents[/]", classes="label")
                yield ListView(id="agents")
                yield Static("", id="detail")
            with Vertical(id="main"):
                yield Log(id="transcript", highlight=True)
                yield Static("", id="status")
        yield Input(placeholder="Ask the agent something…  (Ctrl-R resets, Ctrl-C quits)")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "proteus"
        # Importing the toolset machinery pulls in the provider SDKs and takes a
        # few seconds cold. Say so, rather than showing an empty pane that looks
        # like nothing happened.
        self.sub_title = f"{self.base}  ·  loading…"
        self.query_one("#detail", Static).update("[dim]loading agents…[/]")
        self.load_agents()

    # ── data ─────────────────────────────────────────────────────────────────

    @work(thread=True)
    def load_agents(self) -> None:
        """Read agents from disk, and health from the gateway."""
        try:
            from app import agents_store, toolsets

            agents_store.reset()
            rows = []
            for name, a in sorted(agents_store.store().all().items()):
                tools, _ = toolsets.load_for(a.toolset)
                rows.append({"name": name, "toolset": a.toolset,
                             "description": a.description,
                             "modes": sorted(a.modes),
                             "tools": [t["function"]["name"] for t in (tools or [])]})
        except Exception as exc:
            rows = []
            self.call_from_thread(self.log_line, f"[red]could not read agents: {exc}[/]")

        try:
            health = httpx.get(f"{self.base}/healthz", timeout=5).json()
        except Exception:
            health = {"status": "unreachable"}

        self.call_from_thread(self._apply_agents, rows, health)

    def _apply_agents(self, rows: list[dict], health: dict) -> None:
        self.agents = rows
        view = self.query_one("#agents", ListView)
        view.clear()
        for r in rows:
            view.append(ListItem(Static(f"[b]{r['name']}[/]\n[dim]{len(r['tools'])} tools[/]")))
        colour = {"ok": "green", "degraded": "yellow"}.get(health.get("status"), "red")
        self.sub_title = f"{self.base}  ·  [{colour}]{health.get('status')}[/]  ·  {health.get('model', '?')}"
        if rows and self.current is None:
            view.index = 0
            self._select(0)
        if not rows:
            self.log_line("[yellow]No agents defined.[/]  Create one: proteus agent new <name>")

    def _select(self, index: int) -> None:
        if not (0 <= index < len(self.agents)):
            return
        a = self.agents[index]
        self.current = a["name"]
        self.history.clear()
        detail = (f"[b]{a['name']}[/]\n{a['description'] or '-'}\n\n"
                  f"[b]toolset[/] {a['toolset'] or 'none'}\n"
                  f"[b]tools[/] {', '.join(a['tools']) or '-'}\n")
        if a["modes"]:
            detail += f"[b]modes[/] {', '.join(a['modes'])}\n"
        self.query_one("#detail", Static).update(detail)
        self.log_line(f"\n[dim]── switched to [b]{a['name']}[/b], history cleared ──[/]")

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.index is not None:
            self._select(event.list_view.index)

    # ── chat ─────────────────────────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if self.busy:
            self.log_line("[yellow]still answering — wait for it to finish[/]")
            return
        if not self.current:
            self.log_line("[yellow]pick an agent first[/]")
            return
        self.log_line(f"\n[b cyan]you ›[/] {text}")
        self.history.append({"role": "user", "content": text})
        self.busy = True
        self.ask(text)

    @work(thread=True)
    def ask(self, _text: str) -> None:
        """Stream one turn. In a thread, so the UI keeps repainting."""
        headers = {"Authorization": f"Bearer {self.key}",
                   **signed_headers(self.user),
                   "X-Proteus-Profile": self.current or ""}
        body = {"stream": True, "messages": self.history}
        parts: list[str] = []
        t0 = time.time()
        first: float | None = None
        try:
            with httpx.Client(timeout=300) as client:
                with client.stream("POST", f"{self.base}/v1/chat/completions",
                                   headers=headers, json=body) as r:
                    if r.status_code != 200:
                        detail = r.read().decode("utf-8", "replace")[:200]
                        self.call_from_thread(self.log_line, f"[red]HTTP {r.status_code}[/] {detail}")
                        self.call_from_thread(self._done, None, 0, 0)
                        return
                    self.call_from_thread(self.log_line, "[b green]proteus ›[/]")
                    for line in r.iter_lines():
                        if not line.startswith("data:") or line == "data: [DONE]":
                            continue
                        chunk = json.loads(line[5:])
                        if "proteus_tool_event" in chunk:
                            e = chunk["proteus_tool_event"]
                            mark = "green" if e.get("status") == "ok" else "red"
                            self.call_from_thread(
                                self.log_line,
                                f"  [dim]⚙[/] [{mark}]{e['tool']}[/] [dim]{e.get('status')} "
                                f"{e.get('ms')}ms {str(e.get('query') or '')[:40]}[/]")
                            continue
                        piece = chunk["choices"][0]["delta"].get("content")
                        if piece:
                            if first is None:
                                first = time.time() - t0
                            parts.append(piece)
                            self.call_from_thread(self.stream_text, piece)
        except Exception as exc:
            self.call_from_thread(self.log_line, f"[red]{type(exc).__name__}:[/] {exc}")
        self.call_from_thread(self._done, "".join(parts), first, time.time() - t0)

    def _done(self, text: str | None, first: float | None, total: float) -> None:
        self.busy = False
        if text is None:
            self.history.pop()
            return
        self.history.append({"role": "assistant", "content": text})
        self.last_status = (f"{len(text)} chars · first token {first * 1000:.0f}ms · {total:.1f}s total"
                            if first else f"{total:.1f}s · no text returned")
        self.query_one("#status", Static).update(self.last_status)

    # ── output ───────────────────────────────────────────────────────────────

    def log_line(self, markup: str) -> None:
        self.query_one("#transcript", Log).write_line(_plain(markup))

    def stream_text(self, piece: str) -> None:
        self.query_one("#transcript", Log).write(piece)

    # ── actions ──────────────────────────────────────────────────────────────

    def action_reset(self) -> None:
        self.history.clear()
        self.log_line("\n[dim]── history cleared ──[/]")

    def action_clear(self) -> None:
        self.query_one("#transcript", Log).clear()

    def action_refresh(self) -> None:
        self.load_agents()


def _plain(markup: str) -> str:
    """Log takes plain text; strip the markup we use for the Static widgets."""
    import re

    return re.sub(r"\[/?[a-zA-Z0-9 _#$-]*\]", "", markup)


def run_tui(base: str, key: str, user: str) -> None:
    ProteusTUI(base, key, user).run()
